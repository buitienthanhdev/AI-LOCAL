"""COM helpers for the idempotency marker (registered application + XData) and nothing else. STA thread only."""

from __future__ import annotations

from typing import Any

from lga_worker.autocad import com

MARK_APP = "LGA_OP"  # registered application name of the XData written on every entity a draw tool creates


def register_app(doc: Any, app_name: str = MARK_APP) -> bool:
    """Register the XData application name in the drawing (once). Returns True when it had to be added."""
    apps = doc.RegisteredApplications
    wanted = app_name.casefold()
    for index in range(int(apps.Count)):
        if str(apps.call("Item", index).Name).casefold() == wanted:
            return False
    apps.call("Add", app_name)
    return True


def set_xdata(entity: Any, strings: list[str], app_name: str = MARK_APP) -> None:
    """Attach XData (application name + strings) to an entity. The application must be registered."""
    import pythoncom  # noqa: PLC0415
    from win32com import client  # noqa: PLC0415

    types = client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_I2, [1001] + [1000] * len(strings))
    values = client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [app_name, *strings])
    entity.call("SetXData", types, values)


def get_xdata(entity: Any, app_name: str = MARK_APP) -> list[str] | None:
    """The strings stored with `set_xdata`, or None when the entity carries no XData of this application."""
    import pythoncom  # noqa: PLC0415
    from win32com import client  # noqa: PLC0415

    ole = com.unwrap(entity)
    xtype = client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
    xvalue = client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
    ole.Invoke(ole.GetIDsOfNames("GetXData"), 0, 1, True, app_name, xtype, xvalue)
    values = xvalue.value
    if not values:
        return None
    items = [str(v) for v in values]
    return items[1:] if items and items[0].casefold() == app_name.casefold() else None


def byref_double_pair(obj: Any, method: str) -> tuple[float, float]:
    """Call a COM method whose two arguments are out parameters (doubles), e.g. Layout.GetPaperSize."""
    import pythoncom  # noqa: PLC0415
    from win32com import client  # noqa: PLC0415

    ole = com.unwrap(obj)
    a = client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_R8, 0.0)
    b = client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_R8, 0.0)
    ole.Invoke(ole.GetIDsOfNames(method), 0, 1, True, a, b)
    return float(a.value), float(b.value)


def string_array(values: list[str]) -> Any:
    import pythoncom  # noqa: PLC0415
    from win32com import client  # noqa: PLC0415

    return client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, list(values))
