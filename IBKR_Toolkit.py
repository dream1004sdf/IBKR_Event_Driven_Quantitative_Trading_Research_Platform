from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, W, X, BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


def _project_dir() -> Path:
    # Frozen one-file builds run from a temporary extraction directory. User
    # configuration and the database should remain beside the executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_DIR = _project_dir()


def _cli_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cli", *args]
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.exists():
            executable = console_python
    return [str(executable), str(PROJECT_DIR / "main.py"), *args]


def _hide_own_console() -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


def _dispatch_cli_from_frozen() -> None:
    marker = "--cli"
    if marker not in sys.argv:
        return
    index = sys.argv.index(marker)
    cli_args = sys.argv[index + 1 :]
    sys.argv = ["main.py", *cli_args]
    os.chdir(PROJECT_DIR)
    from main import main

    main()
    raise SystemExit(0)


class ToolkitApp:
    def __init__(self, root: Tk):
        self.root = root
        root.title("IBKR Market Event Recorder & Evaluation Toolkit v0.13")
        root.geometry("1120x760")
        root.minsize(960, 640)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self.start_var = StringVar(value=(date.today() - timedelta(days=365)).isoformat())
        self.end_var = StringVar(value=date.today().isoformat())
        self.event_var = StringVar(value="earnings")
        self.include_logs_var = BooleanVar(value=True)
        self.processes: list[subprocess.Popen[str]] = []

        self._build_ui()
        self._append(
            "IBKR Toolkit ready. Long-running recorder/AI processes open in separate console windows.\n"
            f"Working directory: {PROJECT_DIR}\n"
        )

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=BOTH, expand=True)

        header = ttk.Frame(main)
        header.pack(fill=X)
        ttk.Label(header, text="IBKR 통합 실행기", font=("Segoe UI", 17, "bold")).pack(side=LEFT)
        ttk.Button(header, text="프로젝트 폴더 열기", command=self._open_folder).pack(side=RIGHT)

        notebook = ttk.Notebook(main)
        notebook.pack(fill=X, pady=(10, 8))

        operations = ttk.Frame(notebook, padding=10)
        reports = ttk.Frame(notebook, padding=10)
        replay = ttk.Frame(notebook, padding=10)
        maintenance = ttk.Frame(notebook, padding=10)
        notebook.add(operations, text="실행")
        notebook.add(reports, text="평가 및 보고서")
        notebook.add(replay, text="Historical Replay")
        notebook.add(maintenance, text="점검 및 설정")

        self._button(operations, "Recorder 시작", lambda: self._start_long("run"), 0, 0)
        self._button(operations, "AI Analyzer 시작", lambda: self._start_long("ai-run"), 0, 1)
        self._button(operations, "Recorder + AI 시작", self._start_both, 0, 2)
        self._button(operations, "IBKR 연결 진단 (20초)", lambda: self._run("diagnose", "--seconds", "20"), 1, 0)
        self._button(operations, "Recorder 요약", lambda: self._run("report", "--limit", "30"), 1, 1)
        self._button(operations, "AI 분석 요약", lambda: self._run("ai-report", "--limit", "30"), 1, 2)

        self._button(reports, "Pipeline Report", lambda: self._run("pipeline-report", "--limit", "50"), 0, 0)
        self._button(reports, "Evaluation 실행", lambda: self._run("evaluation-run"), 0, 1)
        self._button(reports, "Evaluation Report", lambda: self._run("evaluation-report", "--show-exclusions"), 0, 2)
        self._button(reports, "Calibration Fit", lambda: self._run("calibration-fit"), 1, 0)
        self._button(reports, "Calibration Report", lambda: self._run("calibration-report"), 1, 1)
        self._button(reports, "통합 System Report", lambda: self._run("system-report"), 1, 2)
        self._button(reports, "Official Readiness", lambda: self._run("official-readiness-report"), 2, 0)
        self._button(reports, "분석 자료 ZIP 만들기", self._create_bundle, 2, 1)
        self._button(reports, "Gate Audit Backfill", lambda: self._run("pipeline-audit-backfill"), 2, 2)
        ttk.Checkbutton(
            reports, text="분석 ZIP에 최근 로그 포함", variable=self.include_logs_var
        ).grid(row=3, column=0, columnspan=2, sticky=W, padx=5, pady=5)

        range_frame = ttk.Frame(replay)
        range_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(range_frame, text="Start").pack(side=LEFT)
        ttk.Entry(range_frame, textvariable=self.start_var, width=13).pack(side=LEFT, padx=(5, 15))
        ttk.Label(range_frame, text="End").pack(side=LEFT)
        ttk.Entry(range_frame, textvariable=self.end_var, width=13).pack(side=LEFT, padx=(5, 15))
        ttk.Label(range_frame, text="Event type").pack(side=LEFT)
        event_box = ttk.Combobox(
            range_frame,
            textvariable=self.event_var,
            values=("earnings", "guidance", "fomc", "cpi", "employment", "macro", "product", "analyst_action"),
            width=18,
        )
        event_box.pack(side=LEFT, padx=5)

        self._button(replay, "Replay Eligibility", self._replay_eligibility, 1, 0)
        self._button(replay, "Replay Seed (AI 호출 없음)", self._replay_seed, 1, 1)
        self._button(replay, "Replay Run", self._replay_run, 1, 2)
        self._button(replay, "Replay Report", lambda: self._run("replay-report"), 2, 0)
        self._button(replay, "Replay Strict Audit", lambda: self._run("replay-audit", "--strict"), 2, 1)

        self._button(maintenance, "Evaluation Migration", lambda: self._run("evaluation-migrate"), 0, 0)
        self._button(maintenance, "DB Audit", lambda: self._run("db-audit", "--strict"), 0, 1)
        self._button(maintenance, "DB Repair", lambda: self._confirm_run("db-repair"), 0, 2)
        self._button(maintenance, "Recorder Self-Test", lambda: self._run("self-test"), 1, 0)
        self._button(maintenance, "AI Self-Test", lambda: self._run("ai-self-test"), 1, 1)
        self._button(maintenance, "전체 Pytest", self._pytest, 1, 2)
        self._button(maintenance, ".env 생성", self._init_env, 2, 0)
        self._button(maintenance, "requirements 설치", self._install_requirements, 2, 1)

        output_header = ttk.Frame(main)
        output_header.pack(fill=X)
        ttk.Label(output_header, text="실행 결과", font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        ttk.Button(output_header, text="출력 저장", command=self._save_output).pack(side=RIGHT)
        ttk.Button(output_header, text="지우기", command=lambda: self.output.delete("1.0", END)).pack(side=RIGHT, padx=5)

        self.output = ScrolledText(main, wrap="word", font=("Consolas", 9))
        self.output.pack(fill=BOTH, expand=True, pady=(5, 0))

        for frame in (operations, reports, replay, maintenance):
            for column in range(3):
                frame.columnconfigure(column, weight=1)

    @staticmethod
    def _button(parent: ttk.Frame, text: str, command, row: int, column: int) -> None:
        ttk.Button(parent, text=text, command=command).grid(
            row=row, column=column, sticky="ew", padx=5, pady=5
        )

    def _append(self, text: str) -> None:
        self.output.insert(END, text)
        self.output.see(END)

    def _run(self, *args: str) -> None:
        command = _cli_command(*args)
        self._append(f"\n> {' '.join(command)}\n")

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=os.environ.copy(),
                )
                assert process.stdout is not None
                for line in process.stdout:
                    self.root.after(0, self._append, line)
                code = process.wait()
                self.root.after(0, self._append, f"[exit={code}]\n")
            except Exception as exc:
                self.root.after(0, self._append, f"ERROR: {exc.__class__.__name__}: {exc}\n")

        threading.Thread(target=worker, daemon=True).start()

    def _start_long(self, command_name: str) -> None:
        command = _cli_command(command_name)
        kwargs: dict[str, object] = {"cwd": PROJECT_DIR, "env": os.environ.copy(), "text": True}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            process = subprocess.Popen(command, **kwargs)
            self.processes.append(process)
            self._append(f"Started PID {process.pid}: {' '.join(command)}\n")
        except Exception as exc:
            messagebox.showerror("실행 오류", f"{exc.__class__.__name__}: {exc}")

    def _start_both(self) -> None:
        self._start_long("run")
        self._start_long("ai-run")

    def _replay_args(self) -> tuple[str, str, str]:
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        event_type = self.event_var.get().strip().lower()
        if not start or not end or not event_type:
            raise ValueError("Start, End, Event type을 모두 입력해야 합니다.")
        return start, end, event_type

    def _replay_eligibility(self) -> None:
        try:
            start, end, event_type = self._replay_args()
        except ValueError as exc:
            messagebox.showwarning("입력 확인", str(exc))
            return
        self._run("replay-eligibility-report", "--start", start, "--end", end, "--event-type", event_type)

    def _replay_seed(self) -> None:
        try:
            start, end, event_type = self._replay_args()
        except ValueError as exc:
            messagebox.showwarning("입력 확인", str(exc))
            return
        self._run("replay-seed", "--start", start, "--end", end, "--event-type", event_type)

    def _replay_run(self) -> None:
        try:
            start, end, event_type = self._replay_args()
        except ValueError as exc:
            messagebox.showwarning("입력 확인", str(exc))
            return
        if not messagebox.askyesno(
            "AI Replay 실행",
            "Historical replay는 AI API를 호출하고 비용이 발생할 수 있습니다. 계속하시겠습니까?",
        ):
            return
        self._run("replay-run", "--start", start, "--end", end, "--event-type", event_type, "--strict")

    def _create_bundle(self) -> None:
        default_name = f"ibkr_analysis_bundle_{date.today().isoformat()}.zip"
        target = filedialog.asksaveasfilename(
            title="분석 자료 ZIP 저장",
            initialdir=str(PROJECT_DIR),
            initialfile=default_name,
            defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip")],
        )
        if not target:
            return
        args = ["analysis-bundle", "--output", target]
        if self.include_logs_var.get():
            args.append("--include-logs")
        self._run(*args)

    def _confirm_run(self, command: str) -> None:
        if messagebox.askyesno("확인", f"{command} 명령을 실행하시겠습니까?"):
            self._run(command)

    def _pytest(self) -> None:
        command = [sys.executable, "-m", "pytest", "-q"]
        if getattr(sys, "frozen", False):
            messagebox.showinfo("Pytest", "Pytest 전체 실행은 소스 폴더의 Python 환경에서 실행하십시오.")
            return
        self._append(f"\n> {' '.join(command)}\n")

        def worker() -> None:
            completed = subprocess.run(command, cwd=PROJECT_DIR, capture_output=True, text=True)
            self.root.after(0, self._append, completed.stdout + completed.stderr + f"[exit={completed.returncode}]\n")

        threading.Thread(target=worker, daemon=True).start()

    def _init_env(self) -> None:
        target = PROJECT_DIR / ".env"
        if target.exists():
            messagebox.showinfo(".env", f"이미 존재합니다: {target}")
            return
        self._run("init-env", "--path", str(target))

    def _install_requirements(self) -> None:
        if getattr(sys, "frozen", False):
            messagebox.showinfo("설치", "EXE 버전은 자체 의존성을 포함합니다. 소스 실행 환경에서만 requirements 설치가 필요합니다.")
            return
        command = [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_DIR / "requirements.txt")]
        self._append(f"\n> {' '.join(command)}\n")

        def worker() -> None:
            completed = subprocess.run(command, cwd=PROJECT_DIR, capture_output=True, text=True)
            self.root.after(0, self._append, completed.stdout + completed.stderr + f"[exit={completed.returncode}]\n")

        threading.Thread(target=worker, daemon=True).start()

    def _open_folder(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(PROJECT_DIR)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(PROJECT_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(PROJECT_DIR)])
        except Exception as exc:
            messagebox.showerror("폴더 열기", str(exc))

    def _save_output(self) -> None:
        target = filedialog.asksaveasfilename(
            title="출력 저장",
            initialdir=str(PROJECT_DIR),
            initialfile="ibkr_toolkit_output.txt",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt")],
        )
        if target:
            Path(target).write_text(self.output.get("1.0", END), encoding="utf-8")

    def _close(self) -> None:
        alive = [process for process in self.processes if process.poll() is None]
        if alive:
            messagebox.showinfo(
                "실행 중인 프로세스",
                "Recorder/AI는 별도 창에서 계속 실행됩니다. 해당 콘솔 창에서 Ctrl+C로 안전하게 종료하십시오.",
            )
        self.root.destroy()


def main() -> None:
    os.chdir(PROJECT_DIR)
    _hide_own_console()
    root = Tk()
    ToolkitApp(root)
    root.mainloop()


if __name__ == "__main__":
    _dispatch_cli_from_frozen()
    main()
