# 安全说明（给用户与信息中心的一页纸）

> desktop-todo v0.2.3 · MIT 许可 · 个人开源项目
> 源码：<https://gitee.com/xinming-chenniang/desktop-todo>（镜像：<https://github.com/xinming-chenniang/desktop-todo-release>）

## 它做什么

在桌面右上角常驻一个待办清单窗口：本地记录、本地保存、回车即记。
数据只写在本机，不联网、不注册账号、不上传任何内容。

## 它不做什么

| 不做 | 可验证方式 |
|---|---|
| 不访问网络 | `grep -nE "AF_INET|urllib|http|requests|getaddrinfo" desktop_todo.py` → **无输出** |
| 不引入第三方库 | 全部 `import` 均为 Python 标准库（见下方命令） |
| 不调用外部命令 | `grep -nE "subprocess|os\.system|Popen" desktop_todo.py` → **无输出** |
| 不采集数据 / 无遥测 | 代码里没有统计、上报、UUID 生成与持久化 |
| 不自动更新 | 没有下载/升级逻辑 |
| 不写系统目录 | 只写下面三处用户目录 |

## 它写哪些文件

| 路径 | 内容 | 何时创建 |
|---|---|---|
| `~/.local/share/desktop-todo/todos.json` | 待办数据（含归档） | 首次运行 |
| `~/.local/share/desktop-todo/hotkey.log` | 唤回排障日志：**窗口激活的时间与状态**，不含按键内容；超 64 KB 轮转为 `.1` | 发生唤回时 |
| `~/.config/autostart/desktop-todo.desktop` | 开机自启 | 仅执行 `--install-autostart` |
| `~/.local/share/applications/desktop-todo.desktop` | 应用菜单入口（Deepin/UOS 需要它来解析应用 id） | 仅执行 `--install-autostart` |
| `$XDG_RUNTIME_DIR/desktop-todo.sock` | 单实例套接字（本地、随会话销毁） | 运行期间 |

## 你可以自己验（不需要信任作者）

```bash
# 1) 没有网络调用：应当无输出
grep -nE "AF_INET|urllib|http|requests|getaddrinfo" desktop_todo.py

# 2) 列出全部 import：应当只有标准库
python3 -c "import ast;print(sorted({a.name for n in ast.walk(ast.parse(open('desktop_todo.py').read())) if isinstance(n,ast.Import) for a in n.names}))"

# 3) 没有子进程：应当无输出
grep -nE "subprocess|os\.system|Popen" desktop_todo.py

# 4) 核对完整性：与发布页的 SHA256SUMS 对照
sha256sum desktop_todo.py
```

`--doctor` 还会逐条回读窗口管理器是否真的接受了「去标题栏 / 不进任务栏」，
把「调用没报错但没生效」这种情况也打出来。

## 卸载（无残留）

```bash
python3 desktop_todo.py --uninstall-autostart       # 删两条 .desktop
rm -rf ~/.local/share/desktop-todo                  # 删数据与日志（需要保留就先拷走）
pkill -f desktop_todo.py                            # 结束进程
```

没有注册表、没有系统目录改动、没有后台服务。

## 依赖与攻击面

- Python 3 + `tkinter`（系统包 `python3-tk`）+ `libX11.so.6`（用标准库 `ctypes` 加载）。
- 没有网络监听端口；唯一套接字是 `AF_UNIX`，权限随用户会话。
- 数据文件是本机普通文件，权限跟随用户家目录；不加密（如需加密，请依赖磁盘加密）。

## 报告问题

安全相关问题请开 issue（不要贴真实待办内容）：
<https://gitee.com/xinming-chenniang/desktop-todo/issues>
