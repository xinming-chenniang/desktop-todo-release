# 隐私说明

> 一页纸，给用户与单位合规用。与 `SECURITY.md` 的分工：这份讲「数据怎么处理」，那份讲「怎么自证安全」。

## 收集什么

**不收集任何数据。** 没有账号、没有遥测、没有统计、没有崩溃上报、没有自动更新。

## 数据存在哪

| 文件 | 内容 | 何时创建 |
|---|---|---|
| `~/.local/share/desktop-todo/todos.json` | 待办数据（含归档） | 首次保存 |
| `~/.local/share/desktop-todo/hotkey.log` | 窗口唤回的时间与状态，**不含按键内容** | 发生唤回时，超 64 KB 轮转 |
| `~/.local/share/desktop-todo/crash.log` | 崩溃现场：版本 / 会话 / Python / traceback，**不含待办正文** | 仅崩溃时 |
| `~/.local/share/desktop-todo/todos.json.broken` | 数据损坏时的原样备份 | 仅检测到损坏 |
| `~/.local/share/desktop-todo/todos.json.import-backup` | 导入前的自动备份 | 仅执行 `--import` |
| `~/.local/share/desktop-todo/todos.json.newer` | 遇到更新版本数据时移走的原文件 | 仅降级打开 |
| `~/.config/autostart/`、`~/.local/share/applications/` | 开机自启 / 菜单入口 | 仅执行 `--install-autostart` |
| 用户指定路径 | `--export` 或归档窗口「导出…」写出的备份 | 仅用户显式操作 |

数据文件是普通文件，**不加密**；需要加密请依赖磁盘/家目录加密。

## 会往外传吗

不会。程序没有网络调用，唯一的套接字是本地 `AF_UNIX`（单实例唤回）。
可自证：

```bash
grep -nE "AF_INET|urllib|http|requests|getaddrinfo" desktop_todo.py   # 无输出
grep -nE "subprocess|os\.system|Popen" desktop_todo.py                # 无输出
```

## 怎么删除

```bash
python3 desktop_todo.py --uninstall-autostart
rm -rf ~/.local/share/desktop-todo
pkill -f desktop_todo.py
```

没有注册表、没有后台服务、没有系统目录残留。

## 第三方

不接入任何第三方服务。运行依赖系统包：`python3-gi`、`gir1.2-gtk-3.0`、`libx11-6`
（由系统包管理器维护，不随本程序分发）。
