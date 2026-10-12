# Attachments: files the user sends with a request

The chat window (`apps/agent-desktop`) lets the user attach files to a message: the 📎 button, drag and drop
anywhere on the window, or pasting an image. Up to 8 files of 25 MB each per message.

## Flow

1. The page encodes the files (base64) and posts them with the request: `POST /api/tasks {task, mode,
   context, attachments: [{name, data_base64}]}`; a reply to a stopped task uses `POST /api/tasks/<id>/resume`
   with the same `attachments` field. A message may consist of files only.
2. `api.rs` saves them under `artifacts/<task_id>/attachments/<safe name>` (names sanitised, duplicates
   suffixed) and starts the CLI with `--attach <path>` for each file (`agent run` and `agent resume` accept it).
3. The CLI copies files that are not already in that folder and records them on the task
   (`TaskRecord.attachments: [{name, path, kind, size}]`, also in the `task_started` audit event).
4. The planner sees a section *Files the user attached to this request* with each path, type and size.
   Images (png/jpg/webp/gif, up to 4) are uploaded to the gateway and attached to every planner turn, so a
   brain that sees images answers from the picture directly; documents are read with `document.read`
   (Word, Excel, PowerPoint, PDF, text, CSV, JSON; images, audio and video through the local media model) or
   `office.*`. An `.xlsx`/`.docx` attachment loads the `office` tool group from the start.
5. The chat shows the attachments in the user's bubble (thumbnails for images, chips that reveal the file in
   Explorer for documents). A follow-up request receives the previous request's attachment paths in its context.

## Limits

* The gateway forwards only png/jpeg/webp/gif as images (12 MB each); BMP/TIFF/HEIC are described through
  `document.read` instead.
* Gateway attachment ids expire after 30 minutes, which is why images are re-uploaded on every turn.
* Policy: `artifacts/` is readable and writable in every mode, so tools can work on the saved copies.
