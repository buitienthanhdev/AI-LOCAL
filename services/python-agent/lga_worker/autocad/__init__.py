"""AutoCAD subsystem: a thin COM (ActiveX) layer over a running AutoCAD, driven from one STA thread.

* `com`      - HRESULT classification, bounded busy retry, VARIANT helpers, instance identity (HWND -> PID).
* `geometry` - entity -> dict normalisation and tolerance-based comparison (pure, unit-testable).
* `provider` - `AutoCadProvider`: every COM call of the subsystem, executed on its own `StaExecutor("autocad")`.

There is deliberately no LISP / SendCommand / keystroke fallback: when COM fails, the tool fails closed.
"""
