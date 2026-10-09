"""Đồng bộ lịch sử Git (connector): commit -> sổ đóng góp + sự kiện GIT_COMMIT. Chỉ ĐỌC (git log), không sửa repo."""
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import contrib, obs
from .catalog import connect

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git_available() -> bool:
    return shutil.which("git") is not None


def _log(repo: str, since_sha: str | None) -> str:
    cmd = ["git", "-C", repo, "-c", "safe.directory=*", "log", "--no-merges", "--shortstat",
           "--pretty=format:%H%x1f%an%x1f%ae%x1f%aI%x1f%s"]
    if since_sha:
        cmd.append(f"{since_sha}..HEAD")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       creationflags=NOWIN)
    return p.stdout if p.returncode == 0 else ""


def parse(output: str) -> list[dict]:
    commits: list[dict] = []
    for line in output.splitlines():
        if "\x1f" in line:
            sha, an, ae, at, subj = (line.split("\x1f") + [""] * 5)[:5]
            commits.append({"sha": sha, "author": an, "email": ae, "at": at, "subject": subj,
                            "files": 0, "ins": 0, "del": 0})
        elif commits and "changed" in line:
            import re
            m = re.search(r"(\d+) files? changed", line)
            i = re.search(r"(\d+) insertions?", line)
            d = re.search(r"(\d+) deletions?", line)
            commits[-1].update(files=int(m.group(1)) if m else 0, ins=int(i.group(1)) if i else 0,
                               **{"del": int(d.group(1)) if d else 0})
    return commits


def sync_repo(conn, repo_id: int, path: str) -> int:
    if not Path(path, ".git").exists():
        return 0
    last = conn.execute("""SELECT sha FROM git_commits WHERE repo_id=? ORDER BY authored_at DESC LIMIT 1""",
                        (repo_id,)).fetchone()
    commits = parse(_log(path, None))  # đầy đủ rồi INSERT OR IGNORE: đơn giản, an toàn khi lịch sử bị viết lại
    name = Path(path).name
    new = 0
    for c in commits:
        cur = conn.execute("""INSERT OR IGNORE INTO git_commits (sha, repo_id, author_name, author_email, authored_at,
                              subject, files_changed, insertions, deletions) VALUES (?,?,?,?,?,?,?,?,?)""",
                           (c["sha"], repo_id, c["author"], c["email"], c["at"], c["subject"], c["files"], c["ins"], c["del"]))
        if cur.rowcount:
            new += 1
            contrib.record_commit(conn, name, c["sha"], c["author"] or c["email"], c["at"], c["subject"], c["ins"],
                                  c["del"], c["files"])
            conn.execute("""INSERT INTO events (event_id, event_type, actor_id, source_system, object_id, ts, last_at,
                            processing_status, created_at, processed_at) VALUES (?,?,?,?,?,?,?, 'done', ?, ?)
                            ON CONFLICT DO NOTHING""",
                         (str(uuid.uuid4()), "GIT_COMMIT", c["author"], "git", f"{path}@{c['sha'][:10]}", time.time(),
                          time.time(), datetime.now().isoformat(timespec="seconds"),
                          datetime.now().isoformat(timespec="seconds")))
    conn.execute("UPDATE repos SET last_sync=? WHERE repo_id=?", (datetime.now().isoformat(timespec="seconds"), repo_id))
    return new


def sync_all() -> dict:
    if not git_available():
        return {"error": "git chưa cài", "repos": 0, "new_commits": 0}
    conn = connect()
    total = 0
    repos = conn.execute("SELECT repo_id, path FROM repos").fetchall()
    for r in repos:
        try:
            total += sync_repo(conn, r["repo_id"], r["path"])
            conn.commit()
        except (subprocess.SubprocessError, OSError):
            obs.metric("error.git_sync", 1)
    conn.close()
    return {"repos": len(repos), "new_commits": total}
