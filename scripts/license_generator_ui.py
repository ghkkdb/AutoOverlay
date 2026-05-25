from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from generate_license import generate_license_file


class LicenseGeneratorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AutoOverlay 授权文件生成器")
        self.geometry("680x300")
        self.minsize(640, 280)

        self.machine_code_var = tk.StringVar()
        self.owner_var = tk.StringVar()
        self.expires_at_var = tk.StringVar(value="never")
        self.private_key_var = tk.StringVar(value=str(Path(__file__).with_name("license_private.pem")))
        self.output_dir_var = tk.StringVar(value=str(Path.cwd()))

        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        self._add_entry_row(frame, 0, "机器码", self.machine_code_var)
        self._add_entry_row(frame, 1, "用户名称", self.owner_var)
        self._add_entry_row(frame, 2, "到期日", self.expires_at_var)

        ttk.Label(frame, text="私钥文件").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.private_key_var).grid(row=3, column=1, sticky="ew", padx=(12, 8), pady=6)
        ttk.Button(frame, text="选择", command=self._choose_private_key).grid(row=3, column=2, pady=6)

        ttk.Label(frame, text="输出目录").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.output_dir_var).grid(row=4, column=1, sticky="ew", padx=(12, 8), pady=6)
        ttk.Button(frame, text="选择", command=self._choose_output_dir).grid(row=4, column=2, pady=6)

        hint = "到期日填写 YYYY-MM-DD，永久授权填写 never。"
        ttk.Label(frame, text=hint).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=6, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(button_frame, text="生成授权文件", command=self._generate).grid(row=0, column=0)

    def _add_entry_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)

    def _choose_private_key(self) -> None:
        path = filedialog.askopenfilename(
            title="选择私钥文件",
            filetypes=[("PEM 私钥", "*.pem"), ("所有文件", "*.*")],
        )
        if path:
            self.private_key_var.set(path)

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir_var.set(path)

    def _generate(self) -> None:
        machine_code = self.machine_code_var.get().strip()
        owner = self.owner_var.get().strip()
        expires_at = self.expires_at_var.get().strip() or "never"
        private_key = Path(self.private_key_var.get().strip())
        output_dir = Path(self.output_dir_var.get().strip())

        if not machine_code:
            messagebox.showerror("参数错误", "请填写机器码。")
            return
        if not owner:
            messagebox.showerror("参数错误", "请填写用户名称。")
            return
        if not private_key.exists():
            messagebox.showerror("参数错误", "私钥文件不存在。")
            return
        if not output_dir.exists() or not output_dir.is_dir():
            messagebox.showerror("参数错误", "输出目录不存在。")
            return

        try:
            output_path = generate_license_file(
                machine_code=machine_code,
                owner=owner,
                expires_at=expires_at,
                private_key_path=private_key,
                output_path=None,
                output_dir=output_dir,
            )
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))
            return

        messagebox.showinfo("生成完成", f"授权文件已生成：\n{output_path}")


if __name__ == "__main__":
    app = LicenseGeneratorApp()
    app.mainloop()
