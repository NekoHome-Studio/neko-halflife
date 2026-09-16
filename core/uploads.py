# -*- coding: utf-8 -*-
"""子插件上传的校验与落盘。

**这是本插件唯一会写入代码并在进程内执行它的地方**，所以校验必须集中在这里、
可单测、且默认拒绝。威胁模型与对策：

* **路径穿越**：上传包里的成员名可能带 ``../`` 或绝对路径，解压后写到插件目录之外。
  → 先校验全部成员名，再落盘；落盘时还用 ``Path.resolve()`` 复核目标确实在目标目录内。
* **压缩炸弹**：``ZipInfo.file_size`` 是包内自报的，可以撒谎。
  → 既查自报总大小，也在写入时按实际字节数二次限流。
* **符号链接**：zip 可以带软链接项，解压后指向任意位置。
  → 拒绝带 Unix symlink 权限位的成员。
* **名字注入**：子插件名会拼进文件路径与 Python 模块名。
  → 白名单校验（必须是合法 Python 标识符），既挡住 ``../``，也避免生成
    含连字符这种 importlib 处理起来很脆的模块名。
* **静默失败**：加载器会跳过以 ``_`` 或 ``.`` 开头的条目，若允许这类名字上传，
  会出现「上传成功但永远不加载」。
  → 名字校验直接拒绝，与加载器的规则对齐。
"""

from __future__ import annotations

import keyword
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

#: 子插件名白名单。必须是合法 Python 标识符：加载器会用这个名字构造模块名。
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

#: 包内允许的最大条目数，防止用海量小文件耗尽 inode。
MAX_ENTRIES = 512

#: Unix 文件类型位与符号链接值（用于识别 zip 里的软链接成员）。
_S_IFMT = 0o170000
_S_IFLNK = 0o120000

_ENTRY_FILE = "__init__.py"


class UploadError(ValueError):
    """上传校验失败。消息会直接回给前端，因此必须是人话。"""


def sanitize_subplugin_name(raw: str) -> str:
    """从上传文件名推导并校验子插件名。

    接受 ``foo.py`` / ``foo.zip`` / ``foo``，统一返回 ``foo``。
    """
    name = str(raw or "").strip()
    if not name:
        raise UploadError("无法从文件名推导子插件名，请检查文件名。")
    # 只取最后一段，挡掉 "a/b.py" 这类伪路径
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".py", ".zip"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.strip()
    if not name:
        raise UploadError("无法从文件名推导子插件名，请检查文件名。")
    if name.startswith(("_", ".")):
        raise UploadError(
            f"子插件名不能以 _ 或 . 开头（{name}）：加载器会跳过这类条目，"
            "会出现上传成功但永远不加载。"
        )
    if not _NAME_RE.match(name):
        raise UploadError(
            f"子插件名 {name!r} 不合法：只能由字母开头，后接字母/数字/下划线，"
            "长度 1~64。这样要求是因为加载器会用它构造 Python 模块名。"
        )
    if keyword.iskeyword(name):
        raise UploadError(f"子插件名 {name!r} 是 Python 关键字，不可使用。")
    return name


def is_safe_zip_member(name: str) -> bool:
    """成员名是否安全（不含绝对路径、盘符或 ``..``）。"""
    normalized = str(name or "").replace("\\", "/")
    if not normalized:
        return False
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    return ".." not in parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & _S_IFMT == _S_IFLNK


def detect_package_root(names: list[str]) -> str:
    """推导包根前缀。

    支持两种布局：成员直接在根（``__init__.py`` 在最外层），或整体套一层目录
    （``my_pack/__init__.py``，常见于 GitHub 打的 zip）。返回要剥掉的前缀，
    根布局返回空串。
    """
    entries = [
        str(name).replace("\\", "/").strip("/")
        for name in names
        if str(name).strip("/")
    ]
    if _ENTRY_FILE in entries:
        return ""
    tops = {entry.split("/", 1)[0] for entry in entries if "/" in entry}
    candidates = [
        top
        for top in tops
        if f"{top}/{_ENTRY_FILE}" in entries
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise UploadError(
            f"压缩包里没找到 {_ENTRY_FILE}。子插件包需要在其根目录提供一个 "
            f"{_ENTRY_FILE}（例如 my_pack/{_ENTRY_FILE} 或直接放在压缩包根）。"
        )
    raise UploadError(
        "压缩包里有多层目录且无法判断哪一个是包根，请把 "
        f"{_ENTRY_FILE} 放在压缩包根目录，或只套一层目录。"
    )


def plan_zip_upload(
    infos: list[zipfile.ZipInfo],
    *,
    max_total_bytes: int,
) -> str:
    """在落盘前校验全部成员，返回包根前缀。

    只做校验、不写任何东西——校验不通过时磁盘上不会留下半个子插件。
    """
    if not infos:
        raise UploadError("压缩包是空的。")
    if len(infos) > MAX_ENTRIES:
        raise UploadError(f"压缩包条目过多（{len(infos)} > {MAX_ENTRIES}）。")

    declared_total = 0
    for info in infos:
        if not is_safe_zip_member(info.filename):
            raise UploadError(f"压缩包内含不安全路径，已拒绝：{info.filename!r}")
        if _is_symlink(info):
            raise UploadError(f"压缩包内含符号链接，已拒绝：{info.filename!r}")
        declared_total += int(info.file_size or 0)
        if max_total_bytes > 0 and declared_total > max_total_bytes:
            raise UploadError(
                f"解压后体积超过上限（{declared_total} > {max_total_bytes} 字节）。"
            )
    return detect_package_root([info.filename for info in infos])


def extract_package(
    zip_path: Path,
    dest_dir: Path,
    *,
    root_prefix: str,
    max_total_bytes: int,
) -> int:
    """把包内 ``root_prefix`` 下的成员解到 ``dest_dir``，返回写入的文件数。

    成员名已由 :func:`plan_zip_upload` 校验过；这里再按实际读出的字节数限流
    （``file_size`` 不可信），并用 ``resolve()`` 复核落点仍在 ``dest_dir`` 内。
    """
    dest_root = dest_dir.resolve()
    written = 0
    actual_total = 0
    prefix = f"{root_prefix}/" if root_prefix else ""

    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            portable = str(info.filename).replace("\\", "/")
            if prefix:
                if not portable.startswith(prefix):
                    continue
                relative = portable[len(prefix) :]
            else:
                relative = portable
            relative = relative.strip("/")
            if not relative or portable.endswith("/"):
                continue

            target = (dest_root / PurePosixPath(relative)).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise UploadError(f"解压目标越界，已中止：{relative!r}")

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    actual_total += len(chunk)
                    if max_total_bytes > 0 and actual_total > max_total_bytes:
                        raise UploadError(
                            "解压时实际体积超过上限，已中止（包内声明的大小不可信，"
                            "可能是压缩炸弹）。"
                        )
                    sink.write(chunk)
            written += 1
    return written


def install_py_file(src: Path, subplugins_dir: Path, name: str) -> Path:
    """安装单文件子插件 ``sub_plugins/<name>.py``。"""
    subplugins_dir.mkdir(parents=True, exist_ok=True)
    dest = subplugins_dir / f"{name}.py"
    if dest.exists():
        raise UploadError(f"子插件 {name} 已存在，请先删除再上传。")
    shutil.copyfile(src, dest)
    return dest


def install_zip_package(
    src: Path,
    subplugins_dir: Path,
    name: str,
    *,
    max_total_bytes: int,
) -> Path:
    """安装包子插件 ``sub_plugins/<name>/``。"""
    if not zipfile.is_zipfile(src):
        raise UploadError("文件不是合法的 zip 压缩包。")
    subplugins_dir.mkdir(parents=True, exist_ok=True)
    dest = subplugins_dir / name
    if dest.exists():
        raise UploadError(f"子插件 {name} 已存在，请先删除再上传。")

    with zipfile.ZipFile(src) as archive:
        root_prefix = plan_zip_upload(
            archive.infolist(), max_total_bytes=max_total_bytes
        )
    dest.mkdir(parents=True)
    try:
        extract_package(
            src,
            dest,
            root_prefix=root_prefix,
            max_total_bytes=max_total_bytes,
        )
    except BaseException:
        # 任何失败都不留半个子插件
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if not (dest / _ENTRY_FILE).is_file():
        shutil.rmtree(dest, ignore_errors=True)
        raise UploadError(f"解压后没找到 {_ENTRY_FILE}，包结构不正确。")
    return dest


def remove_subplugin(subplugins_dir: Path, name: str) -> bool:
    """删除一个子插件（单文件或目录）。返回是否真的删掉了东西。"""
    safe = sanitize_subplugin_name(name)
    removed = False
    file_path = subplugins_dir / f"{safe}.py"
    dir_path = subplugins_dir / safe
    if file_path.is_file():
        file_path.unlink()
        removed = True
    if dir_path.is_dir():
        shutil.rmtree(dir_path)
        removed = True
    return removed
