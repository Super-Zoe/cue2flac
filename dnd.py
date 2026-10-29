"""Windows 原生拖放支持（基于 COM IDropTarget + ctypes，零第三方依赖）。

让任意 Tk 控件成为「文件 / 文件夹」拖放目标：
- enable_drop(widget, on_drop, on_drag_enter=..., on_drag_over=..., on_drag_leave=...)
  把一个 Tk 控件注册为拖放区。拖入（可单文件 / 多文件 / 整个文件夹）后，on_drop 会收到
  「完整路径列表」（文件与文件夹混合）。拖入过程中通过 enter/over/leave 回调做高亮反馈。

非 Windows 平台下 enable_drop 为空操作（浏览按钮仍可用），不影响程序运行。

实现要点：
- 通过 RegisterDragDrop 注册 COM 拖放目标，覆盖 WM_OLE 拖放消息（比 WM_DROPFILES 更完整，
  能拿到 DragEnter/DragOver/DragLeave，从而提供拖入高亮反馈）。
- 在 64 位下，POINTL 按 8 字节整体占位（c_ulonglong），与 Microsoft x64 调用约定匹配。
- IDataObject::GetData 取出 CF_HDROP，用 DragQueryFileW 枚举出每条完整路径。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from ctypes import (
    POINTER,
    CFUNCTYPE,
    Structure,
    Union,
    byref,
    cast,
    c_long,
    c_ulong,
    c_ulonglong,
    c_ubyte,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
import sys

# ---------- 常量 ----------
CF_HDROP = 15
TYMED_HGLOBAL = 1
DVASPECT_CONTENT = 1
DROP_EFFECT_NONE = 0
DROP_EFFECT_COPY = 1

S_OK = 0
E_NOINTERFACE = 0x80004002
DRAGDROP_E_ALREADYREGISTERED = 0x80040101


# ---------- COM 结构 ----------
class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


IID_IUnknown = GUID(
    0x00000000, 0x0000, 0x0000, (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)
)
IID_IDropTarget = GUID(
    0x00000122, 0x0000, 0x0000, (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)
)


class FORMATETC(Structure):
    _fields_ = [
        ("cfFormat", c_ushort),
        ("ptd", c_void_p),
        ("dwAspect", c_ulong),
        ("lindex", c_long),
        ("tymed", c_ulong),
    ]


class STGMEDIUM(Structure):
    class _U(Union):
        _fields_ = [
            ("hGlobal", c_void_p),
            ("hBitmap", c_void_p),
            ("hMetaFilePict", c_void_p),
            ("hEnhMetaFile", c_void_p),
            ("lpszFileName", c_wchar_p),
            ("pstm", c_void_p),
            ("pstg", c_void_p),
        ]

    _anonymous_ = ("_u",)
    _fields_ = [
        ("tymed", c_ulong),
        ("_u", _U),
        ("pUnkForRelease", c_void_p),
    ]


# ---------- 回调签名（必须在引用它们的结构/类之前定义） ----------
_QI = CFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_ADDREF = CFUNCTYPE(c_ulong, c_void_p)
_REL = CFUNCTYPE(c_ulong, c_void_p)
# POINTL 在 64 位下作为一个 8 字节值传入（R9 寄存器），用 c_ulonglong 占位。
_DRAGENTER = CFUNCTYPE(c_long, c_void_p, c_void_p, c_ulong, c_ulonglong, POINTER(c_ulong))
_DRAGOVER = CFUNCTYPE(c_long, c_void_p, c_ulong, c_ulonglong, POINTER(c_ulong))
_DRAGLEAVE = CFUNCTYPE(c_long, c_void_p)
_DROP = CFUNCTYPE(c_long, c_void_p, c_void_p, c_ulong, c_ulonglong, POINTER(c_ulong))
_GETDATA = CFUNCTYPE(c_long, c_void_p, POINTER(FORMATETC), POINTER(STGMEDIUM))


class IDropTargetVtbl(Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _REL),
        ("DragEnter", _DRAGENTER),
        ("DragOver", _DRAGOVER),
        ("DragLeave", _DRAGLEAVE),
        ("Drop", _DROP),
    ]


class _DropTargetStruct(Structure):
    """COM 对象布局：首个字段为虚表指针，随后是自定义的 Python 侧数据。"""

    _fields_ = [
        ("lpVtbl", POINTER(IDropTargetVtbl)),
        ("refcount", c_long),
        ("hwnd", c_void_p),
    ]


def _guid_eq(a: GUID, b: GUID) -> bool:
    return (
        a.Data1 == b.Data1
        and a.Data2 == b.Data2
        and a.Data3 == b.Data3
        and bytes(a.Data4) == bytes(b.Data4)
    )


def _extract_paths(pDataObj) -> list[str]:
    """从 IDataObject 取出 CF_HDROP 中的全部完整路径（文件 + 文件夹）。失败返回空列表。"""
    if not pDataObj:
        return []
    try:
        fmt = FORMATETC()
        fmt.cfFormat = CF_HDROP
        fmt.dwAspect = DVASPECT_CONTENT
        fmt.lindex = -1
        fmt.tymed = TYMED_HGLOBAL
        medium = STGMEDIUM()
        medium.tymed = TYMED_HGLOBAL

        # pDataObj 是 COM 接口指针：*pDataObj 才是虚表指针，再 [3] 才是 GetData
        vtbl_ptr = cast(pDataObj, POINTER(c_void_p))
        vtable = cast(vtbl_ptr.contents, POINTER(c_void_p * 12))
        getdata = cast(vtable.contents[3], _GETDATA)  # IDataObject::GetData 位于虚表第 3 项
        hr = getdata(pDataObj, byref(fmt), byref(medium))
        if hr != S_OK:
            return []
        hdrop = medium.hGlobal
        if not hdrop:
            ctypes.windll.ole32.ReleaseStgMedium(byref(medium))
            return []

        shell32 = ctypes.windll.shell32
        shell32.DragQueryFileW.argtypes = [c_void_p, c_ulong, c_wchar_p, c_ulong]
        shell32.DragQueryFileW.restype = c_ulong
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)

        buf = ctypes.create_unicode_buffer(4096)
        paths: list[str] = []
        for i in range(count):
            shell32.DragQueryFileW(hdrop, i, buf, 4096)
            val = buf.value
            if val:
                paths.append(val)
        ctypes.windll.ole32.ReleaseStgMedium(byref(medium))
        return paths
    except Exception:
        return []


class DropTarget:
    """把一个 Tk 控件包装成 COM 拖放目标。"""

    _registry: dict[int, "DropTarget"] = {}

    def __init__(
        self,
        widget,
        on_drop,
        on_drag_enter=None,
        on_drag_over=None,
        on_drag_leave=None,
    ) -> None:
        self.widget = widget
        self.on_drop = on_drop
        self.on_drag_enter = on_drag_enter
        self.on_drag_over = on_drag_over
        self.on_drag_leave = on_drag_leave
        self.hwnd = int(widget.winfo_id())

        self.struct = _DropTargetStruct()
        self.struct.refcount = 1
        self.struct.hwnd = self.hwnd

        # 保持 CFUNCTYPE 实例存活
        self._qi = _QI(self._qi_cb)
        self._addref = _ADDREF(self._addref_cb)
        self._rel = _REL(self._rel_cb)
        self._dragenter = _DRAGENTER(self._dragenter_cb)
        self._dragover = _DRAGOVER(self._dragover_cb)
        self._dragleave = _DRAGLEAVE(self._dragleave_cb)
        self._drop = _DROP(self._drop_cb)

        self.vtbl = IDropTargetVtbl(
            QueryInterface=self._qi,
            AddRef=self._addref,
            Release=self._rel,
            DragEnter=self._dragenter,
            DragOver=self._dragover,
            DragLeave=self._dragleave,
            Drop=self._drop,
        )
        self.struct.lpVtbl = POINTER(IDropTargetVtbl)(self.vtbl)

        DropTarget._registry[self.hwnd] = self

        ole32 = ctypes.windll.ole32
        ole32.OleInitialize.argtypes = [c_void_p]
        ole32.OleInitialize.restype = c_long
        ole32.OleInitialize(None)
        ole32.RegisterDragDrop.argtypes = [c_void_p, c_void_p]
        ole32.RegisterDragDrop.restype = c_long
        hr = ole32.RegisterDragDrop(
            self.hwnd, cast(ctypes.byref(self.struct), c_void_p)
        )
        if hr not in (S_OK, DRAGDROP_E_ALREADYREGISTERED):
            raise ctypes.WinError(hr)

    # ---- COM 方法 ----
    def _qi_cb(self, this, riid, ppv):
        if riid and ppv:
            g = riid.contents
            if _guid_eq(g, IID_IDropTarget) or _guid_eq(g, IID_IUnknown):
                ppv.contents.value = this
                self._addref_cb(this)
                return S_OK
            ppv.contents.value = None
        return E_NOINTERFACE

    def _addref_cb(self, this):
        self.struct.refcount += 1
        return self.struct.refcount

    def _rel_cb(self, this):
        self.struct.refcount -= 1
        return self.struct.refcount

    def _dragenter_cb(self, this, pDataObj, keystate, pt, pdwEffect):
        paths = _extract_paths(pDataObj)
        ok = len(paths) > 0
        if ok and self.on_drag_enter:
            self.on_drag_enter()
        if pdwEffect:
            pdwEffect.contents.value = DROP_EFFECT_COPY if ok else DROP_EFFECT_NONE
        return S_OK

    def _dragover_cb(self, this, keystate, pt, pdwEffect):
        if self.on_drag_over:
            self.on_drag_over()
        if pdwEffect:
            pdwEffect.contents.value = DROP_EFFECT_COPY
        return S_OK

    def _dragleave_cb(self, this):
        if self.on_drag_leave:
            self.on_drag_leave()
        return S_OK

    def _drop_cb(self, this, pDataObj, keystate, pt, pdwEffect):
        paths = _extract_paths(pDataObj)
        if self.on_drag_leave:
            self.on_drag_leave()
        if self.on_drop:
            try:
                self.on_drop(paths)
            except Exception:
                # 拖放回调内的异常不应冒泡到 OLE 消息循环
                pass
        if pdwEffect:
            pdwEffect.contents.value = DROP_EFFECT_COPY if paths else DROP_EFFECT_NONE
        return S_OK


def enable_drop(
    widget,
    on_drop,
    on_drag_enter=None,
    on_drag_over=None,
    on_drag_leave=None,
):
    """把一个 Tk 控件注册为文件/文件夹拖放目标。

    参数
    ----
    widget          : Tk 控件（需有有效 HWND，如 ttk.Entry / tk.Frame 等）。
    on_drop         : 回调，签名 on_drop(paths: list[str])，paths 为拖入的完整路径列表。
    on_drag_enter   : 可选，拖入开始（用于高亮）。
    on_drag_over    : 可选，拖入移动中（用于持续高亮）。
    on_drag_leave   : 可选，拖出/放下后（用于取消高亮）。

    返回 DropTarget 实例，非 Windows 平台返回 None（不影响程序运行）。
    """
    if sys.platform != "win32":
        return None
    try:
        return DropTarget(
            widget,
            on_drop,
            on_drag_enter=on_drag_enter,
            on_drag_over=on_drag_over,
            on_drag_leave=on_drag_leave,
        )
    except Exception as e:  # noqa: BLE001
        # 拖放只是便利功能，初始化失败不应阻断主程序
        print(f"拖放初始化失败（已降级为仅浏览按钮）：{e}")
        return None


# 使 `from dnd import enable_drop` 更直观
__all__ = ["enable_drop", "DropTarget"]
