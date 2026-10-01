import queue
import sys
import threading
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

import discord_bot
import roblox_monitor
from config_manager import load_config, save_config


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

STATUS_ORDER = {"Kicked": 0, "Frozen": 0, "Reopening": 1, "Closed": 2, "Playing": 3}


class RedirectStdout:
    """Send worker-thread output to a queue that Tk drains on its own thread."""

    def __init__(self, output_queue):
        self.output_queue = output_queue

    def write(self, value):
        if value:
            self.output_queue.put(value)
        return len(value)

    def flush(self):
        return None


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Roblox Auto-Rejoiner")
        self.geometry("780x820")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.config_data = load_config()
        self.monitor_thread = None
        self.socket_thread = None
        self.bot_thread = None
        self.monitor_running = False
        self.bot_running = False
        self._closing = False
        self._original_stdout = sys.stdout
        self._console_queue = queue.SimpleQueue()
        self._last_accounts_text = None

        self._create_widgets()
        sys.stdout = RedirectStdout(self._console_queue)
        self.after(50, self._drain_console)
        self.after(1000, self._refresh_accounts)

        print("Welcome to Roblox Auto-Rejoiner!")
        print("Start the monitor: it watches every open Roblox client and reopens only the ones that get kicked.")

    def _create_widgets(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self,
            text="Roblox Auto-Rejoiner",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(15, 5))

        config_frame = ctk.CTkFrame(self, corner_radius=10)
        config_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        config_frame.grid_columnconfigure((1, 3), weight=1)

        self.var_place_id = ctk.StringVar(value=str(self.config_data.get("PLACE_ID", "")).replace("PUT_PLACE_ID_HERE", ""))
        self.var_rejoin_delay = ctk.StringVar(value=str(self.config_data.get("REJOIN_DELAY", 5)))
        self.var_bot_token = ctk.StringVar(value=self.config_data.get("BOT_TOKEN", ""))
        self.var_channel_id = ctk.StringVar(value=str(self.config_data.get("CHANNEL_ID") or ""))
        self.var_reopen_closed = ctk.BooleanVar(value=bool(self.config_data.get("REOPEN_CLOSED", False)))

        fields = (
            ("Place ID (optional):", self.var_place_id, 0, 0, 200, None),
            ("Rejoin Delay (s):", self.var_rejoin_delay, 0, 2, 80, None),
            ("Bot Token:", self.var_bot_token, 1, 0, 200, "*"),
            ("Channel ID:", self.var_channel_id, 1, 2, 150, None),
        )
        for label, variable, row, column, width, mask in fields:
            ctk.CTkLabel(
                config_frame,
                text=label,
                font=ctk.CTkFont(weight="bold"),
            ).grid(row=row, column=column, padx=(20, 10), pady=10, sticky="w")
            ctk.CTkEntry(
                config_frame,
                textvariable=variable,
                width=width,
                show=mask or "",
            ).grid(
                row=row,
                column=column + 1,
                padx=(10, 20) if column == 2 else 10,
                pady=10,
                sticky="ew" if width == 200 else "w",
            )

        ctk.CTkCheckBox(
            config_frame,
            text="Also reopen accounts whose window closes or crashes (not only kicks)",
            variable=self.var_reopen_closed,
        ).grid(row=2, column=0, columnspan=4, padx=20, pady=(0, 5), sticky="w")

        ctk.CTkButton(
            config_frame,
            text="Save Configuration",
            command=self.save_cfg,
            fg_color="#2FA572",
            hover_color="#106A43",
        ).grid(row=3, column=0, columnspan=4, pady=(10, 20))

        control_frame = ctk.CTkFrame(self, fg_color="transparent")
        control_frame.grid(row=2, column=0, padx=20, pady=5, sticky="ew")
        control_frame.grid_columnconfigure((0, 1), weight=1)

        monitor_frame = ctk.CTkFrame(control_frame, fg_color="transparent")
        monitor_frame.grid(row=0, column=0, sticky="new", padx=10)
        self.btn_monitor = ctk.CTkButton(
            monitor_frame,
            text="Start Monitor",
            command=self.toggle_monitor,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.btn_monitor.pack(fill="x", pady=(0, 5))
        self.lbl_mon_status = ctk.CTkLabel(
            monitor_frame,
            text="Offline",
            text_color="#C8504B",
            font=ctk.CTkFont(weight="bold"),
        )
        self.lbl_mon_status.pack()

        bot_frame = ctk.CTkFrame(control_frame, fg_color="transparent")
        bot_frame.grid(row=0, column=1, sticky="new", padx=10)
        self.btn_bot = ctk.CTkButton(
            bot_frame,
            text="Start Discord Bot",
            command=self.toggle_bot,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.btn_bot.pack(fill="x", pady=(0, 5))
        self.lbl_bot_status = ctk.CTkLabel(
            bot_frame,
            text="Offline",
            text_color="#C8504B",
            font=ctk.CTkFont(weight="bold"),
        )
        self.lbl_bot_status.pack()
        self.btn_test = ctk.CTkButton(
            bot_frame,
            text="Send Test Message",
            command=self.send_test_message,
            height=28,
            fg_color="transparent",
            border_width=1,
        )
        self.btn_test.pack(fill="x", pady=(5, 0))

        accounts_frame = ctk.CTkFrame(self, corner_radius=10)
        accounts_frame.grid(row=3, column=0, padx=20, pady=(10, 0), sticky="ew")
        ctk.CTkLabel(
            accounts_frame,
            text="Accounts",
            font=ctk.CTkFont(weight="bold"),
        ).pack(pady=(10, 0), padx=10, anchor="w")
        self.accounts_box = ctk.CTkTextbox(
            accounts_frame,
            height=150,
            wrap="none",
            font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled",
            fg_color="#1e1e1e",
            text_color="#d4d4d4",
        )
        self.accounts_box.pack(fill="x", padx=10, pady=(5, 10))

        console_frame = ctk.CTkFrame(self, corner_radius=10)
        console_frame.grid(row=4, column=0, padx=20, pady=10, sticky="nsew")
        ctk.CTkLabel(
            console_frame,
            text="Console Output",
            font=ctk.CTkFont(weight="bold"),
        ).pack(pady=(10, 0), padx=10, anchor="w")
        self.console = ctk.CTkTextbox(
            console_frame,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled",
            fg_color="#1e1e1e",
            text_color="#d4d4d4",
        )
        self.console.pack(fill="both", expand=True, padx=10, pady=(5, 10))

    def _drain_console(self):
        if self._closing:
            return
        chunks = []
        while True:
            try:
                chunks.append(self._console_queue.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self.console.configure(state="normal")
            self.console.insert("end", "".join(chunks))
            self.console.see("end")
            self.console.configure(state="disabled")
        self.after(50, self._drain_console)

    def _refresh_accounts(self):
        if self._closing:
            return
        with roblox_monitor.state_lock:
            accounts = [dict(acct) for acct in roblox_monitor.state["accounts"].values()]
        if not accounts:
            text = "No Roblox clients seen yet." if self.monitor_running else "Start the monitor to see your accounts."
        else:
            accounts.sort(key=lambda acct: (STATUS_ORDER.get(acct.get("status"), 9), str(acct.get("name", "")).lower()))
            rows = [f"{'Account':<22}{'Status':<12}{'Kicks':>6}{'Reopened':>10}  Note"]
            for acct in accounts:
                note = acct.get("detail") or ""
                if not note and acct.get("last_kick"):
                    when = acct["last_kick"][11:16]
                    try:
                        when = datetime.fromisoformat(acct["last_kick"]).strftime("%I:%M %p").lstrip("0")
                    except ValueError:
                        pass
                    note = f"last kick {when}: {acct.get('last_message') or acct.get('last_reason') or '?'}"
                rows.append(
                    f"{str(acct.get('name', '?'))[:21]:<22}{str(acct.get('status', '?')):<12}"
                    f"{acct.get('kicks', 0):>6}{acct.get('rejoins', 0):>10}  {note}"
                )
            text = "\n".join(rows)
        if text != self._last_accounts_text:
            self._last_accounts_text = text
            self.accounts_box.configure(state="normal")
            self.accounts_box.delete("1.0", "end")
            self.accounts_box.insert("end", text)
            self.accounts_box.configure(state="disabled")
        self.after(1000, self._refresh_accounts)

    def save_cfg(self):
        try:
            place_id = self.var_place_id.get().strip()
            if place_id and not place_id.isdigit():
                raise ValueError("Place ID must contain digits only (or be left empty)")
            rejoin_delay = float(self.var_rejoin_delay.get())
            if rejoin_delay < 0:
                raise ValueError("Rejoin delay cannot be negative")
            channel = self.var_channel_id.get().strip()
            if channel and not channel.isdigit():
                raise ValueError("Channel ID must contain digits only")

            self.config_data.update(
                PLACE_ID=place_id or "PUT_PLACE_ID_HERE",
                REJOIN_DELAY=rejoin_delay,
                BOT_TOKEN=self.var_bot_token.get().strip(),
                CHANNEL_ID=int(channel) if channel else None,
                REOPEN_CLOSED=bool(self.var_reopen_closed.get()),
            )
            if not save_config(self.config_data):
                raise OSError("Could not write config.json")
            roblox_monitor.reload_monitor_config()
            discord_bot.reload_bot_config()
            print("Configuration saved.")
            messagebox.showinfo("Success", "Configuration saved.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Invalid configuration", str(exc))

    def toggle_monitor(self):
        if not self.monitor_running:
            self.monitor_running = True
            roblox_monitor.MONITOR_ACTIVE = True
            roblox_monitor.SOCKET_ACTIVE = True
            with roblox_monitor.state_lock:
                roblox_monitor.state["monitoring"] = True

            self.btn_monitor.configure(
                text="Stop Monitor",
                fg_color="#C8504B",
                hover_color="#8E3531",
            )
            self.lbl_mon_status.configure(text="Running", text_color="#2FA572")

            if not self.socket_thread or not self.socket_thread.is_alive():
                self.socket_thread = threading.Thread(target=roblox_monitor.socket_server, daemon=True)
                self.socket_thread.start()
            self.monitor_thread = threading.Thread(target=self._run_monitor, daemon=True)
            self.monitor_thread.start()
        else:
            self._stop_monitor()

    def _run_monitor(self):
        try:
            roblox_monitor.monitor_loop()
        except Exception as exc:
            print(f"Monitor error: {exc}")
        finally:
            if not self._closing:
                self.after(0, self._set_monitor_stopped)

    def _stop_monitor(self):
        roblox_monitor.MONITOR_ACTIVE = False
        roblox_monitor.SOCKET_ACTIVE = False
        self._set_monitor_stopped()
        print("Stopping monitor services...")

    def _set_monitor_stopped(self):
        self.monitor_running = False
        self.btn_monitor.configure(
            text="Start Monitor",
            fg_color=["#3a7ebf", "#1f538d"],
            hover_color=["#325882", "#14375e"],
        )
        self.lbl_mon_status.configure(text="Offline", text_color="#C8504B")

    def toggle_bot(self):
        if not self.bot_running:
            token = self.config_data.get("BOT_TOKEN", "").strip()
            if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
                messagebox.showerror("Invalid configuration", "Set a Discord bot token first.")
                return
            self.bot_running = True
            discord_bot.reload_bot_config()
            self.btn_bot.configure(
                text="Stop Discord Bot",
                fg_color="#C8504B",
                hover_color="#8E3531",
            )
            self.lbl_bot_status.configure(text="Running", text_color="#2FA572")
            self.bot_thread = threading.Thread(target=self._run_bot, daemon=True)
            self.bot_thread.start()
        else:
            self.bot_running = False
            self._set_bot_stopped()
            threading.Thread(target=discord_bot.stop_bot_sync, daemon=True).start()

    def _run_bot(self):
        try:
            discord_bot.client.run(discord_bot.BOT_TOKEN)
        except Exception as exc:
            print(f"Discord bot error: {exc}")
        finally:
            if not self._closing:
                self.after(0, self._set_bot_stopped)

    def _set_bot_stopped(self):
        self.bot_running = False
        self.btn_bot.configure(
            text="Start Discord Bot",
            fg_color=["#3a7ebf", "#1f538d"],
            hover_color=["#325882", "#14375e"],
        )
        self.lbl_bot_status.configure(text="Offline", text_color="#C8504B")

    def send_test_message(self):
        self.btn_test.configure(state="disabled", text="Sending...")
        threading.Thread(target=self._send_test_worker, daemon=True).start()

    def _send_test_worker(self):
        ok, message = discord_bot.send_test_message_sync()
        if not self._closing:
            self.after(0, self._test_done, ok, message)

    def _test_done(self, ok, message):
        self.btn_test.configure(state="normal", text="Send Test Message")
        print(("Test message: " if ok else "Test message failed: ") + message)
        if ok:
            messagebox.showinfo("Discord", message)
        else:
            messagebox.showerror("Discord", message)

    def close_app(self):
        self._closing = True
        roblox_monitor.MONITOR_ACTIVE = False
        roblox_monitor.SOCKET_ACTIVE = False
        if self.bot_running:
            threading.Thread(target=discord_bot.stop_bot_sync, daemon=True).start()
        sys.stdout = self._original_stdout
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
