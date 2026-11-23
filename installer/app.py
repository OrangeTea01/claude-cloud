"""Windows desktop entry point. Importing this module never starts a connection."""
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

if __package__:
    from .core import Cancelled, Deployer, DeploymentError, LocalState, Settings
else:
    from core import Cancelled, Deployer, DeploymentError, LocalState, Settings


def app_directory():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class InstallerApp:
    def __init__(self, root, base=None, state_path=None):
        self.root = root
        self.base = Path(base) if base else app_directory()
        self.state_path = Path(state_path) if state_path else Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'ClaudeCloudOpenSourceInstaller' / 'state.json'
        self.events = queue.Queue()
        self.deployer = None
        self.busy = False
        self.closing = False
        self.url = ''
        self.inputs = []
        self.fields = {name: tk.StringVar(value=default) for name, default in {
            'host': '', 'port': '22', 'username': 'root', 'auth': 'password', 'ssh_password': '',
            'key_path': '', 'key_passphrase': '', 'sudo_password': '', 'site_password': '',
            'confirm_password': '', 'address': '', 'profile': 'auto'}.items()}
        self.style()
        self.build()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def style(self):
        self.root.title('Claude Cloud · 开源独享版部署')
        self.root.geometry('860x850')
        self.root.minsize(700, 680)
        self.root.configure(bg='#faf9f5')
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background='#faf9f5', foreground='#171717', font=('Microsoft YaHei UI', 10))
        style.configure('TEntry', fieldbackground='#ffffff', foreground='#171717', padding=8)
        style.configure('TCombobox', fieldbackground='#ffffff', padding=6)
        style.configure('TButton', padding=(14, 9), background='#ffffff', bordercolor='#dedbd2')
        style.configure('Accent.TButton', background='#e8b19b', foreground='#171717', padding=(20, 11), font=('Microsoft YaHei UI', 11, 'bold'))
        style.map('Accent.TButton', background=[('active', '#e3a98f'), ('disabled', '#e9e6dd')])
        style.configure('Heading.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        style.configure('Sub.TLabel', foreground='#45413d', font=('Microsoft YaHei UI', 9))
        style.configure('TProgressbar', background='#c48a73', troughcolor='#eeece5', bordercolor='#eeece5')

    def entry(self, host, row, label, name, *, secret=False, hint=None):
        ttk.Label(host, text=label).grid(row=row, column=0, sticky='w', pady=(8, 4), padx=(0, 16))
        value = ttk.Entry(host, textvariable=self.fields[name], show='●' if secret else '')
        value.grid(row=row, column=1, sticky='ew', pady=(8, 4))
        self.inputs.append(value)
        if hint:
            ttk.Label(host, text=hint, style='Sub.TLabel', wraplength=550).grid(row=row + 1, column=1, sticky='w', pady=(0, 4))
        return value

    def build(self):
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='把你的 Claude，放到自己的云端。', style='Heading.TLabel').pack(anchor='w')
        ttk.Label(outer, text='全新 Debian 12 / 13 · 8 GB 起 · 一个网站密码，独享工作区', style='Sub.TLabel').pack(anchor='w', pady=(8, 16))
        canvas_frame = ttk.Frame(outer)
        canvas_frame.pack(fill='both', expand=True)
        canvas = tk.Canvas(canvas_frame, bg='#faf9f5', highlightthickness=0)
        scroll = ttk.Scrollbar(canvas_frame, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        content = ttk.Frame(canvas, padding=(0, 0, 12, 8))
        window_id = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        def wheel(event):
            canvas.yview_scroll(-int(event.delta / 120), 'units')
        self.root.bind('<MouseWheel>', wheel)
        form = ttk.Frame(content)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.entry(form, 0, '服务器公网 IP', 'host', hint='也可填 SSH 域名；不要加 https://。')
        self.entry(form, 2, 'SSH 用户名', 'username', hint='通常为 root；Debian 镜像也可能为 debian。')
        ttk.Label(form, text='SSH 登录方式').grid(row=4, column=0, sticky='w', pady=(8, 4))
        auth = ttk.Frame(form)
        auth.grid(row=4, column=1, sticky='w', pady=(8, 4))
        for title, value in (('服务器密码', 'password'), ('私钥文件', 'key')):
            button = ttk.Radiobutton(auth, text=title, variable=self.fields['auth'], value=value, command=self.switch_auth)
            button.pack(side='left', padx=(0, 22))
            self.inputs.append(button)
        self.password_box = ttk.Frame(form)
        self.password_box.grid(row=5, column=0, columnspan=2, sticky='ew')
        self.password_box.columnconfigure(1, weight=1)
        self.entry(self.password_box, 0, 'SSH 密码', 'ssh_password', secret=True)
        self.key_box = ttk.Frame(form)
        self.key_box.grid(row=6, column=0, columnspan=2, sticky='ew')
        self.key_box.columnconfigure(1, weight=1)
        self.entry(self.key_box, 0, '私钥文件', 'key_path')
        browse = ttk.Button(self.key_box, text='选择文件', command=self.browse_key)
        browse.grid(row=0, column=2, padx=(8, 0), pady=(8, 4))
        self.inputs.append(browse)
        self.entry(self.key_box, 1, '私钥口令（可空）', 'key_passphrase', secret=True)
        self.key_box.grid_remove()
        ttk.Separator(form).grid(row=7, column=0, columnspan=2, sticky='ew', pady=12)
        self.entry(form, 8, '网站登录密码', 'site_password', secret=True, hint='至少 12 个字符。以后只用此密码进入网站，无需用户名。')
        self.entry(form, 10, '再输入一次', 'confirm_password', secret=True)
        self.advanced_button = ttk.Button(form, text='▸ 更多设置（域名、端口、sudo、服务器配置）', command=self.toggle_advanced)
        self.advanced_button.grid(row=11, column=0, columnspan=2, sticky='w', pady=(14, 2))
        self.inputs.append(self.advanced_button)
        self.advanced = ttk.Frame(form)
        self.advanced.grid(row=12, column=0, columnspan=2, sticky='ew')
        self.advanced.columnconfigure(1, weight=1)
        self.entry(self.advanced, 0, '网站域名（可空）', 'address', hint='空白则使用服务器 IP。域名须提前解析到此服务器。')
        self.entry(self.advanced, 2, 'SSH 端口', 'port')
        self.entry(self.advanced, 3, 'sudo 密码（可空）', 'sudo_password', secret=True,
                   hint='root 不需要。普通用户留空时先使用 SSH 密码；无密码 sudo 可直接运行。')
        ttk.Label(self.advanced, text='服务器配置').grid(row=5, column=0, sticky='w', pady=(8, 4))
        profile = ttk.Combobox(self.advanced, textvariable=self.fields['profile'], values=('auto', '8g', '12g', '24g'), state='readonly', width=12)
        profile.grid(row=5, column=1, sticky='w', pady=(8, 4))
        self.inputs.append(profile)
        ttk.Label(self.advanced, text='auto 自动检查内存并选择档位。', style='Sub.TLabel').grid(row=6, column=1, sticky='w')
        self.advanced.grid_remove()
        ttk.Label(content, text='请先在云控制台放行 TCP 80、443 和 SSH 端口。首次连接需要核对服务器指纹。\n无需激活码。部署文件随包提供，服务器密码和网站密码仅经 SSH 传输。\n不会覆盖未知网站；失败后用同一电脑、同一服务器、相同设置与首次网站密码继续。',
                  style='Sub.TLabel', wraplength=730).pack(anchor='w', pady=(16, 10))
        self.status = tk.StringVar(value='填写完成后，点击开始部署。')
        ttk.Label(content, textvariable=self.status, wraplength=730).pack(anchor='w', pady=(6, 8))
        self.progress = ttk.Progressbar(content, maximum=6)
        self.progress.pack(fill='x', pady=(0, 10))
        self.log = tk.Text(content, height=7, wrap='word', bg='#ffffff', fg='#171717', relief='flat', borderwidth=1,
                           font=('Consolas', 9), padx=10, pady=10, state='disabled')
        self.log.pack(fill='x')
        self.result = ttk.Frame(content)
        self.result.pack(fill='x', pady=(10, 0))
        self.result_text = tk.StringVar()
        ttk.Label(self.result, textvariable=self.result_text, wraplength=730).pack(anchor='w')
        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(15, 0))
        self.start_button = ttk.Button(actions, text='开始部署', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.cancel_button = ttk.Button(actions, text='停止连接', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left', padx=10)
        self.open_button = ttk.Button(actions, text='打开网站', command=self.open_site, state='disabled')
        self.open_button.pack(side='right')
        self.copy_button = ttk.Button(actions, text='复制网址', command=self.copy_url, state='disabled')
        self.copy_button.pack(side='right', padx=8)
        self.fields['host'].trace_add('write', lambda *_: self.result_text.set('') if not self.busy else None)

    def switch_auth(self):
        if self.fields['auth'].get() == 'key':
            self.password_box.grid_remove()
            self.key_box.grid()
        else:
            self.key_box.grid_remove()
            self.password_box.grid()

    def toggle_advanced(self):
        visible = bool(self.advanced.winfo_manager())
        if visible:
            self.advanced.grid_remove()
        else:
            self.advanced.grid()
        self.advanced_button.configure(text=('▸' if visible else '▾') + ' 更多设置（域名、端口、sudo、服务器配置）')

    def browse_key(self):
        value = filedialog.askopenfilename(title='选择服务器私钥', filetypes=[('OpenSSH / PEM 私钥', '*'), ('PEM', '*.pem')])
        if value:
            self.fields['key_path'].set(value)

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.inputs:
            widget.configure(state='disabled' if busy else 'readonly' if isinstance(widget, ttk.Combobox) else 'normal')
        self.start_button.configure(state='disabled' if busy else 'normal', text='正在部署…' if busy else '开始部署 / 继续')
        self.cancel_button.configure(state='normal' if busy else 'disabled')

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 1200:
            self.log.delete('1.0', '200.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self):
        if self.busy:
            return
        try:
            settings = Settings.from_form({key: value.get() for key, value in self.fields.items()})
            state = LocalState(self.state_path)
        except DeploymentError as error:
            messagebox.showerror('请检查填写内容', str(error), parent=self.root)
            return
        self.set_busy(True)
        self.url = ''
        self.result_text.set('')
        self.open_button.configure(state='disabled')
        self.copy_button.configure(state='disabled')
        self.progress['value'] = 0
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self.status.set('准备校验文件并连接服务器…')
        def emit(kind, **values):
            self.events.put((kind, values))
        def confirm(host, digest):
            ready = threading.Event()
            answer = {'accepted': False}
            self.events.put(('confirm_host', {'host': host, 'digest': digest, 'ready': ready, 'answer': answer}))
            while not ready.wait(.1):
                if self.deployer.cancelled.is_set():
                    return False
            return answer['accepted']
        self.deployer = Deployer(self.base, state, emit, confirm)
        def worker():
            try:
                self.deployer.run(settings)
            except Cancelled as error:
                emit('cancelled', message=str(error))
            except DeploymentError as error:
                emit('failed', message=str(error))
            except Exception:
                emit('failed', message='程序未能完成当前步骤。可保留部署包，并使用相同信息重试。')
            finally:
                settings.erase_secrets()
                emit('finished')
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        while True:
            try:
                kind, values = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'confirm_host':
                values['answer']['accepted'] = messagebox.askyesno('首次连接：核对服务器身份',
                    '服务器：%s\n\nSSH 公钥指纹：\n%s\n\n请与云控制台中的服务器 SSH 指纹核对。确认是你的服务器后继续。\n此公钥会保存在本机；以后如果变化，程序会停止连接。' % (values['host'], values['digest']), parent=self.root)
                values['ready'].set()
            elif kind == 'log':
                self.append_log(values.get('message', ''))
            elif kind == 'progress':
                self.status.set(values.get('message', '正在部署…'))
                self.progress['value'] = values.get('step', 0)
                self.append_log(values.get('message', ''))
            elif kind == 'complete':
                self.url = values['url']
                self.progress['value'] = 6
                self.status.set('部署完成，网站已通过云端可信 HTTPS 检查。')
                note = '本机访问检查也已通过。' if values.get('local_ok') else '本机暂未连通，请检查本机网络后打开；云端证书和网站检查已通过。'
                self.result_text.set(self.url + '\n' + note + '\n下一步：打开网站，输入网站密码，在菜单中连接 Claude。官方验证码和授权需由你本人完成。')
                self.open_button.configure(state='normal')
                self.copy_button.configure(state='normal')
            elif kind in {'failed', 'cancelled'}:
                self.status.set('部署尚未完成。' if kind == 'failed' else '已停止连接。')
                self.append_log(values['message'])
                self.result_text.set(values['message'])
            elif kind == 'finished':
                self.set_busy(False)
                for field in ('ssh_password', 'sudo_password', 'key_passphrase', 'site_password', 'confirm_password'):
                    self.fields[field].set('')
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)

    def cancel(self):
        if self.busy and messagebox.askyesno('停止连接', '停止后云端已经完成的步骤会保留，安装进程可能仍在继续。\n使用相同设置和第一次的网站密码可以继续。现在停止连接？', parent=self.root):
            self.deployer.cancel()
            self.status.set('正在停止连接…')

    def close(self):
        if self.closing:
            return
        if self.busy and not messagebox.askyesno('部署尚未结束', '关闭会断开连接，云端步骤可能继续。稍后可以重新打开程序继续。是否关闭？', parent=self.root):
            return
        if self.deployer and self.busy:
            # Wait until the worker closes SSH and cleans its temporary uploads.
            self.closing = True
            self.deployer.cancel()
        for field in ('ssh_password', 'sudo_password', 'key_passphrase', 'site_password', 'confirm_password'):
            self.fields[field].set('')
        if self.closing:
            self.status.set('正在停止连接并清理临时上传，请稍候…')
            return
        self.root.destroy()

    def open_site(self):
        if self.url:
            webbrowser.open(self.url)

    def copy_url(self):
        if self.url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.url)
            self.status.set('网址已复制。')


def main():
    root = tk.Tk()
    InstallerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
