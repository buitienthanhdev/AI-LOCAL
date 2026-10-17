"""Handlers for the office.* tools (contract: tools/office.json): Excel, Word, PowerPoint and Outlook through
their own COM automation, i.e. the real applications with the user's documents, mailbox and settings.

* Excel/Word: read and write the live document (also one the user has open), formulas are calculated by Excel.
* PDF export of any Word, Excel or PowerPoint file.
* Outlook: list and read mail, create a draft (shown to the user), send (the contract makes sending always
  ask the user first).

Every COM call runs on one STA thread. Office applications started here are hidden and closed again when
they were not running before; documents the user already had open are left open.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paths import absolute_path
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.sta import StaExecutor

BACKEND = "office-com"
SERVICE_NAME = "office"
MAX_CELLS = 5000
MAX_TEXT_CHARS = 30_000
XL_PDF = 0  # xlTypePDF
WD_PDF = 17  # wdExportFormatPDF
PP_PDF = 2  # ppFixedFormatTypePDF
OL_INBOX = 6  # olFolderInbox
OL_SENT = 5
OL_DRAFTS = 16
FOLDERS = {"inbox": OL_INBOX, "sent": OL_SENT, "drafts": OL_DRAFTS}


class Office:
    def __init__(self) -> None:
        self.executor = StaExecutor("office")

    # ------------------------------------------------------------------ helpers (STA thread)
    @staticmethod
    def _app(prog_id: str) -> tuple[Any, bool]:
        """(application, started_here)."""
        import win32com.client

        try:
            return win32com.client.GetActiveObject(prog_id), False
        except Exception:
            app = win32com.client.DispatchEx(prog_id)
            try:
                app.Visible = False
                app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable: never run macros of documents opened here (NOT TESTED live)
                app.DisplayAlerts = False if prog_id != "PowerPoint.Application" else app.DisplayAlerts
            except Exception:
                pass
            return app, True

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return str(Path(a).resolve()).lower() == str(Path(b).resolve()).lower()

    def _workbook(self, path: str | None) -> tuple[Any, Any, bool, bool]:
        """(excel, workbook, excel_started_here, workbook_opened_here)."""
        excel, started = self._app("Excel.Application")
        if not path:
            book = excel.ActiveWorkbook
            if book is None:
                raise ToolError(ErrorCode.NOT_FOUND, "no workbook is open in Excel; give a path")
            return excel, book, started, False
        target = absolute_path(path)
        for book in excel.Workbooks:
            if self._same(book.FullName, str(target)):
                return excel, book, started, False
        if not target.is_file():
            book = excel.Workbooks.Add()
            book.SaveAs(str(target))
            return excel, book, started, True
        return excel, excel.Workbooks.Open(str(target)), started, True

    @staticmethod
    def _sheet(book: Any, sheet: str | None) -> Any:
        if not sheet:
            return book.ActiveSheet
        for ws in book.Worksheets:
            if ws.Name.lower() == sheet.lower():
                return ws
        ws = book.Worksheets.Add(After=book.Worksheets(book.Worksheets.Count))
        ws.Name = sheet
        return ws

    @staticmethod
    def _finish(app: Any, started: bool, doc: Any | None, opened: bool, save: bool) -> None:
        if doc is not None and save:
            doc.Save()
        if doc is not None and opened:
            doc.Close(False)
        if started:
            try:
                app.Quit()
            except Exception:
                pass

    # ------------------------------------------------------------------ Excel
    def excel_read(self, path: str | None, sheet: str | None, cells: str | None) -> dict[str, Any]:
        excel, book, started, opened = self._workbook(path)
        try:
            ws = self._sheet(book, sheet)
            rng = ws.Range(cells) if cells else ws.UsedRange
            if rng.Count > MAX_CELLS:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{rng.Count} cells; read at most {MAX_CELLS} (give a smaller range)")
            values = rng.Value
            if not isinstance(values, tuple):
                values = ((values,),)
            rows = [["" if v is None else _cell_text(v) for v in row] for row in values]
            formulas = rng.Formula
            if not isinstance(formulas, tuple):
                formulas = ((formulas,),)
            with_formulas = {}
            top, left = rng.Row, rng.Column
            for r, row in enumerate(formulas):
                for c, f in enumerate(row):
                    if isinstance(f, str) and f.startswith("="):
                        with_formulas[ws.Cells(top + r, left + c).Address.replace("$", "")] = f
            return {"workbook": book.FullName, "sheet": ws.Name, "range": rng.Address.replace("$", ""), "rows": rows,
                    "formulas": with_formulas, "sheets": [s.Name for s in book.Worksheets]}
        finally:
            self._finish(excel, started, book, opened, save=False)

    def excel_write(self, path: str | None, sheet: str | None, start: str, values: list[list[Any]], save: bool) -> dict[str, Any]:
        excel, book, started, opened = self._workbook(path)
        try:
            ws = self._sheet(book, sheet)
            width = max((len(r) for r in values), default=0)
            if not values or not width:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "values must be a non-empty 2-D list")
            first = ws.Range(start)
            for r, row in enumerate(values):
                for c, value in enumerate(row):
                    cell = ws.Cells(first.Row + r, first.Column + c)
                    if isinstance(value, str) and value.startswith("="):
                        cell.Formula = value
                    else:
                        cell.Value = value
            excel.Calculate()
            last = ws.Cells(first.Row + len(values) - 1, first.Column + width - 1)
            rng = ws.Range(first, last)
            back = rng.Value
            if not isinstance(back, tuple):
                back = ((back,),)
            result = {"workbook": book.FullName, "sheet": ws.Name, "range": rng.Address.replace("$", ""),
                      "values_after": [["" if v is None else v for v in row] for row in back], "saved": bool(save or opened)}
            return result
        finally:
            self._finish(excel, started, book, opened, save=save or opened)

    # ------------------------------------------------------------------ Word
    def _document(self, path: str) -> tuple[Any, Any, bool, bool]:
        word, started = self._app("Word.Application")
        target = absolute_path(path)
        for doc in word.Documents:
            if self._same(doc.FullName, str(target)):
                return word, doc, started, False
        if not target.is_file():
            doc = word.Documents.Add()
            doc.SaveAs2(str(target))
            return word, doc, started, True
        return word, word.Documents.Open(str(target)), started, True

    def word_read(self, path: str) -> dict[str, Any]:
        word, doc, started, opened = self._document(path)
        try:
            text = doc.Content.Text.replace("\r", "\n")
            return {"document": doc.FullName, "paragraphs": doc.Paragraphs.Count, "text": text[:MAX_TEXT_CHARS],
                    "truncated": len(text) > MAX_TEXT_CHARS}
        finally:
            self._finish(word, started, doc, opened, save=False)

    def word_write(self, path: str, append: list[str], replace: dict[str, str], heading: str | None) -> dict[str, Any]:
        word, doc, started, opened = self._document(path)
        try:
            replaced = 0
            for old, new in replace.items():
                replaced += doc.Content.Text.count(old)
                find = doc.Content.Find
                find.ClearFormatting()
                find.Replacement.ClearFormatting()
                # Execute(FindText, MatchCase, MatchWholeWord, MatchWildcards, MatchSoundsLike, MatchAllWordForms,
                #         Forward, Wrap=wdFindContinue, Format, ReplaceWith, Replace=wdReplaceAll)
                find.Execute(old, True, False, False, False, False, True, 1, False, new, 2)
            if heading:
                rng = doc.Content
                rng.InsertParagraphAfter()
                para = doc.Paragraphs(doc.Paragraphs.Count).Range
                para.Text = heading
                para.Style = doc.Styles(-2)  # wdStyleHeading1
            for line in append:
                rng = doc.Content
                rng.InsertParagraphAfter()
                para = doc.Paragraphs(doc.Paragraphs.Count).Range
                para.Text = line
                para.Style = doc.Styles(-1)  # wdStyleNormal
            text = doc.Content.Text
            return {"document": doc.FullName, "replaced": replaced, "appended": len(append) + (1 if heading else 0),
                    "contains_appended": all(line.strip() in text for line in append), "saved": True}
        finally:
            self._finish(word, started, doc, opened, save=True)

    # ------------------------------------------------------------------ PDF
    def export_pdf(self, path: str, output: str) -> dict[str, Any]:
        source = absolute_path(path)
        target = absolute_path(output)
        if not source.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"{source} does not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = source.suffix.lower()
        if ext in (".doc", ".docx", ".rtf", ".odt"):
            word, doc, started, opened = self._document(str(source))
            try:
                doc.ExportAsFixedFormat(str(target), WD_PDF)
            finally:
                self._finish(word, started, doc, opened, save=False)
        elif ext in (".xls", ".xlsx", ".xlsm", ".csv"):
            excel, book, started, opened = self._workbook(str(source))
            try:
                book.ExportAsFixedFormat(XL_PDF, str(target))
            finally:
                self._finish(excel, started, book, opened, save=False)
        elif ext in (".ppt", ".pptx"):
            pp, started = self._app("PowerPoint.Application")
            deck = pp.Presentations.Open(str(source), True, False, False)
            try:
                deck.SaveAs(str(target), 32)  # ppSaveAsPDF
            finally:
                deck.Close()
                if started:
                    pp.Quit()
        else:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"cannot export {ext} files; Word, Excel and PowerPoint only")
        ok = target.is_file() and target.read_bytes()[:5] == b"%PDF-"
        return {"source": str(source), "pdf": str(target), "size": target.stat().st_size if ok else 0, "valid_pdf": ok}

    # ------------------------------------------------------------------ Outlook
    @staticmethod
    def _outlook() -> Any:
        import win32com.client

        try:
            return win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            return win32com.client.Dispatch("Outlook.Application")

    @staticmethod
    def _mail_summary(item: Any) -> dict[str, Any]:
        def get(name: str, default: Any = "") -> Any:
            try:
                return getattr(item, name)
            except Exception:
                return default

        body = str(get("Body") or "")
        return {"entry_id": get("EntryID"), "subject": get("Subject"), "from": get("SenderName"),
                "from_address": get("SenderEmailAddress"), "to": get("To"), "received": str(get("ReceivedTime")),
                "unread": bool(get("UnRead", False)), "attachments": int(get("Attachments").Count) if get("Attachments", None) else 0,
                "preview": " ".join(body.split())[:200]}

    def outlook_list(self, folder: str, count: int, unread_only: bool, search: str | None) -> dict[str, Any]:
        ns = self._outlook().GetNamespace("MAPI")
        box = ns.GetDefaultFolder(FOLDERS.get(folder, OL_INBOX))
        items = box.Items
        items.Sort("[ReceivedTime]", True)
        out: list[dict[str, Any]] = []
        needle = (search or "").lower()
        for item in items:
            if len(out) >= count:
                break
            if getattr(item, "Class", 0) != 43:  # olMail
                continue
            summary = self._mail_summary(item)
            if unread_only and not summary["unread"]:
                continue
            if needle and needle not in f"{summary['subject']} {summary['from']} {summary['preview']}".lower():
                continue
            out.append(summary)
        return {"folder": box.Name, "messages": out}

    def outlook_read(self, entry_id: str) -> dict[str, Any]:
        ns = self._outlook().GetNamespace("MAPI")
        try:
            item = ns.GetItemFromID(entry_id)
        except Exception as exc:
            raise ToolError(ErrorCode.NOT_FOUND, f"no mail with entry_id {entry_id}") from exc
        summary = self._mail_summary(item)
        body = str(item.Body or "")
        summary["body"] = body[:MAX_TEXT_CHARS]
        summary["attachment_names"] = [item.Attachments.Item(i + 1).FileName for i in range(item.Attachments.Count)]
        return summary

    def outlook_compose(self, to: str, subject: str, body: str, cc: str | None, attachments: list[str], send: bool) -> dict[str, Any]:
        mail = self._outlook().CreateItem(0)  # olMailItem
        mail.To = to
        if cc:
            mail.CC = cc
        mail.Subject = subject
        mail.Body = body
        for path in attachments:
            p = absolute_path(path)
            if not p.is_file():
                raise ToolError(ErrorCode.NOT_FOUND, f"attachment {p} does not exist")
            mail.Attachments.Add(str(p))
        if send:
            mail.Send()
            return {"sent": True, "to": to, "subject": subject}
        mail.Save()
        mail.Display(False)
        return {"sent": False, "draft_entry_id": mail.EntryID, "to": to, "subject": subject}


async def _office(ctx: ToolContext) -> Office:
    async def create() -> Office:
        office = Office()
        office.executor.start()
        return office

    return await ctx.services.get_or_create(SERVICE_NAME, create)


async def _run(ctx: ToolContext, fn, *args: Any) -> Any:
    office = await _office(ctx)
    ctx.check_cancelled()
    try:
        return await office.executor.run(max(1.0, ctx.remaining_s() - 1.0), fn, *args)
    except ToolError:
        raise
    except Exception as exc:  # COM errors carry the application's own message
        text = str(exc)
        try:
            text = exc.excepinfo[2] or text  # type: ignore[attr-defined]
        except Exception:
            pass
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"Office: {text}") from exc


# Excel hands error cells over as large negative integers (COM): name them, a formula that ends in #DIV/0! is not done.
EXCEL_ERRORS = {-2146826281: "#DIV/0!", -2146826246: "#N/A", -2146826259: "#NAME?", -2146826288: "#NULL!",
                -2146826252: "#NUM!", -2146826265: "#REF!", -2146826273: "#VALUE!"}


def _cell_text(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool) and v in EXCEL_ERRORS:
        return EXCEL_ERRORS[v]
    return v if isinstance(v, (int, float, bool)) else str(v)


def _error_cells(rows: list[list[Any]]) -> list[str]:
    return [f"row {r + 1} col {c + 1} {v}" for r, row in enumerate(rows) for c, v in enumerate(row) if isinstance(v, str) and v in EXCEL_ERRORS.values()]


def _table(rows: list[list[Any]], limit: int = 40) -> str:
    lines = [" | ".join(str(v) for v in row) for row in rows[:limit]]
    more = f"\n... {len(rows) - limit} more rows" if len(rows) > limit else ""
    return "\n".join(lines) + more


def register(registry: ToolRegistry) -> None:
    from lga_worker.office import tools as spec_tools

    spec_tools.register(registry)

    @registry.handler("office.excel_read")
    async def excel_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.excel_read, args.get("path"), args.get("sheet"), args.get("range"))
        formulas = "; ".join(f"{k}{v}" for k, v in list(data["formulas"].items())[:20])
        return ToolOutcome(data=data, backend=BACKEND,
                           observation=f"{data['workbook']} [{data['sheet']}] {data['range']} (sheets: {', '.join(data['sheets'])}):\n"
                           f"{_table(data['rows'])}" + (f"\nFormulas: {formulas}" if formulas else "")
                           + (f"\nWARNING: error values in the range ({'; '.join(_error_cells(data['rows'])[:6])}): the formulas that produce them are NOT working, "
                              "so the task is not done; look at the cells they refer to." if _error_cells(data['rows']) else ""))

    @registry.handler("office.excel_write")
    async def excel_write(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.excel_write, args.get("path"), args.get("sheet"), args.get("start", "A1"),
                          args["values"], bool(args.get("save", True)))
        expected = [[v for v in row] for row in args["values"]]
        literal_ok = all(
            str(want) == str(got) or (isinstance(want, (int, float)) and isinstance(got, (int, float)) and abs(want - got) < 1e-9)
            for want_row, got_row in zip(expected, data["values_after"]) for want, got in zip(want_row, got_row)
            if not (isinstance(want, str) and want.startswith("="))
        )
        return ToolOutcome(data=data, backend=BACKEND, checks=[Check("cells_read_back", literal_ok, f"{data['range']} read back")],
                           observation=f"Wrote {data['range']} in {data['workbook']} [{data['sheet']}]; now:\n{_table(data['values_after'])}")

    @registry.handler("office.word_read")
    async def word_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.word_read, args["path"])
        return ToolOutcome(data=data, backend=BACKEND,
                           observation=f"{data['document']} ({data['paragraphs']} paragraphs{', truncated' if data['truncated'] else ''}):\n{data['text'][:8000]}")

    @registry.handler("office.word_write")
    async def word_write(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.word_write, args["path"], args.get("append", []), args.get("replace", {}), args.get("heading"))
        return ToolOutcome(data=data, backend=BACKEND,
                           checks=[Check("appended_text_present", data["contains_appended"], "document text read back")],
                           observation=f"{data['document']}: {data['replaced']} replacement(s), {data['appended']} paragraph(s) added, saved.")

    @registry.handler("office.export_pdf")
    async def export_pdf(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.export_pdf, args["path"], args["output"])
        return ToolOutcome(data=data, backend=BACKEND, checks=[Check("valid_pdf", data["valid_pdf"], f"{data['size']} bytes")],
                           artifacts=[{"kind": "file", "path": data["pdf"]}],
                           observation=f"Exported {data['source']} -> {data['pdf']} ({data['size']} bytes).")

    @registry.handler("office.outlook_list")
    async def outlook_list(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.outlook_list, args.get("folder", "inbox"), int(args.get("count", 15)),
                          bool(args.get("unread_only", False)), args.get("search"))
        lines = [f"[{m['entry_id'][-12:]}] {m['received'][:16]} {'*' if m['unread'] else ' '} {m['from']}: {m['subject']} — {m['preview'][:90]}"
                 for m in data["messages"]]
        return ToolOutcome(data=data, backend=BACKEND,
                           observation=f"{data['folder']}: {len(lines)} message(s) (* = unread; use the full entry_id from data to read):\n"
                           + "\n".join(lines))

    @registry.handler("office.outlook_read")
    async def outlook_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.outlook_read, args["entry_id"])
        return ToolOutcome(data=data, backend=BACKEND,
                           observation=f"From {data['from']} <{data['from_address']}> to {data['to']}, {data['received']}\n"
                           f"Subject: {data['subject']}\nAttachments: {', '.join(data['attachment_names']) or 'none'}\n\n{data['body'][:8000]}")

    @registry.handler("office.outlook_draft")
    async def outlook_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.outlook_compose, args["to"], args["subject"], args["body"], args.get("cc"),
                          args.get("attachments", []), False)
        return ToolOutcome(data=data, backend=BACKEND, checks=[Check("draft_saved", bool(data.get("draft_entry_id")), "saved in Drafts")],
                           observation=f"Draft to {data['to']} '{data['subject']}' saved in Drafts and opened for the user to review.")

    @registry.handler("office.outlook_send")
    async def outlook_send(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        office = await _office(ctx)
        data = await _run(ctx, office.outlook_compose, args["to"], args["subject"], args["body"], args.get("cc"),
                          args.get("attachments", []), True)
        return ToolOutcome(data=data, backend=BACKEND, checks=[Check("sent", data["sent"], "Outlook accepted the message")],
                           observation=f"Sent '{data['subject']}' to {data['to']} through Outlook.")
