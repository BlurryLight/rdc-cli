# rdc-cli 离线安装包（Windows x64 / CPython 3.12）

> 给内网 AI/人用的说明。**不需要编译 RenderDoc，不需要 venv，不需要联网。**
> 由 `BlurryLight/rdc-cli` 的 `Offline bundle` 流水线自动构建。
> 执行顺序从上到下；每一步都有期望输出，对不上再看第 7 节排错。

包内容：

| 路径 | 说明 |
|---|---|
| `wheels/` | `rdc-cli {{RDC_VERSION}}` + 依赖（`{{RDC_PYTHON}}` + win_amd64 的 wheel） |
| `payload/win-x64/` | 预编译 RenderDoc **{{RDOC_VERSION}}** 模块：`renderdoc.pyd`(`{{RDC_PYTHON}}`)、`renderdoc.dll`、`renderdoccmd.exe`、VC 运行库、Android APK ×2 |
| `payload/win-x64/MANIFEST.json` | 版本/commit + 每个 payload 文件的 sha256（安装脚本会自动校验） |
| `requirements.txt` | 精确版本锁（由构建时的 wheel 文件名生成） |
| `MANIFEST.sha256` | 本包所有文件的 sha256，可先做完整性自检 |
| `install_offline.py` | 安装脚本：校验 → 复制 payload → 写环境变量 → 离线 pip 安装 |

---

## 0. 硬性前提（不满足就别往下走）

| 检查 | 命令 | 要求 |
|---|---|---|
| 系统 | `[Environment]::Is64BitOperatingSystem` | `True`（只支持 Windows x64） |
| Python | `python -c "import sys;print(sys.version)"` | **必须是 3.12.x**，64 位 |
| pip | `python -m pip --version` | 有即可；没有就 `python -m ensurepip --upgrade` |

为什么死锁 3.12：`renderdoc.pyd` 是 SWIG 原生扩展，ABI 绑定 CPython **{{RDC_PYTHON}}**。3.11/3.13 都会在
`import renderdoc` 时报 `DLL load failed` / `ModuleNotFoundError`，**换版本只能重新编译，改环境变量无效**。

内网如果只有别的 Python 版本：装一个 3.12（官方 embeddable/installer 都行），然后
`python install_offline.py --python C:\Python312\python.exe`。

---

## 1. 一键安装

```powershell
# 在包根目录执行（PowerShell）
python install_offline.py --verify-bundle   # 先确认传输没损坏：期望「包完整性校验通过：N 个文件」
python install_offline.py
```

它会做 4 件事（幂等，重复执行安全）：

1. 按 `MANIFEST.json` 校验 payload 的 sha256；
2. 复制到 `%LOCALAPPDATA%\rdc\renderdoc`（模块 + renderdoccmd + VC 运行库）和
   `%LOCALAPPDATA%\rdc\share\renderdoc\plugins\android`（APK）；
3. `pip install --no-index --find-links wheels -r requirements.txt`（不碰网络）；
4. 写用户环境变量 `RENDERDOC_PYTHON_PATH=%LOCALAPPDATA%\rdc\renderdoc`。

期望结尾：

```
[rdc] 模块与 APK：9 个文件更新
[rdc] pip install --no-index ...
Successfully installed ...
[rdc] 已设置用户环境变量 RENDERDOC_PYTHON_PATH=...\rdc\renderdoc（新进程生效）
[rdc] 完成。下一步：
[rdc]   1) C:\...\Scripts\rdc.exe --version   （或新开终端后 rdc --version）
```

常见变体：

```powershell
python install_offline.py --verify-bundle         # 只校验包完整性（传输损坏检查）
python install_offline.py --check                 # 只校验（部署后验收用）
python install_offline.py --no-pip                # 只铺 payload，rdc-cli 自己处理
python install_offline.py --force                 # 强制重铺 payload
python install_offline.py --pip-args "--user"     # 无写权限时装到用户目录
python install_offline.py --no-env                # 不写环境变量（由组策略/外部管理）
```

> `--check` 退出码 0 = 全部就绪，1 = 有漂移。CI/自动化里直接看退出码。

---

## 2. 手动等价步骤（脚本不可用时的退路）

```powershell
# 2.1 复制模块
$dst = "$env:LOCALAPPDATA\rdc\renderdoc"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item .\payload\win-x64\renderdoc\* $dst -Force

# 2.2 复制 Android APK（不抓 Android 可跳过）
$apk = "$env:LOCALAPPDATA\rdc\share\renderdoc\plugins\android"
New-Item -ItemType Directory -Force $apk | Out-Null
Copy-Item .\payload\win-x64\android\*.apk $apk -Force

# 2.3 装 Python 包（完全离线）
python -m pip install --no-index --find-links .\wheels -r .\requirements.txt

# 2.4 写环境变量（当前进程立即生效；新终端才永久生效）
[Environment]::SetEnvironmentVariable("RENDERDOC_PYTHON_PATH", $dst, "User")
$env:RENDERDOC_PYTHON_PATH = $dst
```

若内网有 pip 镜像，2.3 也可用 `-i http://<mirror>/simple`，但**不要**升级/降级
`rdc-cli` 或它的依赖到其它版本，版本组合是本包验证过的。

---

## 3. 验收

```powershell
rdc --version                 # 期望：rdc, version {{RDC_VERSION}}
rdc doctor                    # 期望下面这些行都是 [ok]
```

```
[ok] python: 3.12.x
[ok] platform: windows
[ok] renderdoc-module: version={{RDOC_VERSION}}
[ok] replay-support: renderdoc replay API surface found
[ok] renderdoccmd: ...\rdc\renderdoc\renderdoccmd.exe (renderdoccmd x64 v{{RDOC_VERSION}} built from {{RDOC_COMMIT}}...)
[ok] win-renderdoc-install: RenderDoc found at ...\rdc\renderdoc\renderdoc.dll
[ok] android-apk: 2 APK(s) at ...\plugins\android
[ok] renderdoc-variant: upstream ({{RDOC_VERSION}})
```

`android-apk` 那行出现 `AndroidAPKVerifyFailed` 说明 APK 与宿主版本不匹配 —— 本包内是同一
{{RDOC_VERSION}}/`{{RDOC_COMMIT}}`，对不上说明文件被替换过，重铺 payload。

`win-vulkan-layer` 行是**可选**的：本包不注册 Vulkan 抓帧层（回放不需要），内网机器没有
系统 RenderDoc 时这一行可能缺失或告警，不影响回放/Android。

如果 `rdc` 不在 PATH（`%USERPROFILE%\.local\bin` 或 `Scripts` 不在 PATH），用全路径：

```powershell
& (Get-Command python).Source -c "import sysconfig;print(sysconfig.get_path('scripts'))"
# 该目录下就有 rdc.exe
```

---

## 4. 安装 skill（给 AI 用）

```powershell
rdc install-skill          # 写入 ~/.claude/skills/rdc-cli/
rdc install-skill --check  # 期望：Skill files are installed and current.
```

skill 文件（`SKILL.md` + 命令速查）随 wheel 一起分发，**离线可用**。
如果机器上 `~/.claude/skills` 是指向 `~/.agents/skills` 的软链（dotfiles 布局），会自然落到位；
不需要额外配置。

---

## 5. 装完能干什么（冒烟测试）

```powershell
rdc doctor
rdc android setup                     # 没设备时提示 no Android devices 属正常，说明 adb/模块 OK
rdc open .\some.rdc                   # 回放本机抓的帧
rdc draws | Select-Object -First 5
rdc passes
rdc close
```

桌面抓帧仍需要官方 RenderDoc GUI（本包只含**回放**所需的模块，不含 Qt GUI）；
Android 抓帧用包内 APK：

```powershell
rdc android setup
rdc android capture com.example.app/.MainActivity -o frame.rdc
rdc open frame.rdc --android
rdc draws
rdc close ; rdc android stop
```

---

## 6. 边界（包里没有什么）

| 不含 | 原因 / 替代 |
|---|---|
| venv / uv | 不需要；包装进当前 Python 即可 |
| RenderDoc GUI / 抓帧 Vulkan 层 | 回放不需要；桌面抓帧用系统安装的官方 RenderDoc |
| 源码编译链（VS Build Tools） | 用不上，已预编译 |
| `rdc setup-renderdoc` 产物 | **不要执行该命令**，它会尝试重新编译（10–20 分钟且需 MSVC） |
| macOS / Linux / arm64 模块 | 本包只有 win-x64 |

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `需要 CPython 3.12，当前是 3.11` | 装 3.12，或 `--python C:\Python312\python.exe` |
| `import renderdoc` → `DLL load failed while importing renderdoc` | Python 不是 3.12 或不是 64 位；`--check` 会指出 |
| `ModuleNotFoundError: No module named 'renderdoc'` | `RENDERDOC_PYTHON_PATH` 没生效（当前进程）→ 新开终端，或 `$env:RENDERDOC_PYTHON_PATH=...` |
| payload 校验失败（sha256 不符） | 传输损坏；重新拷包后 `--verify-bundle` |
| `AndroidAPKVerifyFailed` | APK 与 pyd 版本不同；重铺 `payload/win-x64/android/*.apk` |
| pip 提示找不到包 | 必须带 `--no-index --find-links wheels`；别让 pip 去联网 |
| pip 权限错误 | `--pip-args "--user"`，或用管理员 Python 安装 |
| `rdc doctor` 的 renderdoccmd 指向系统旧版 | 优先用 `RENDERDOC_PYTHON_PATH` 同级目录的 `renderdoccmd.exe`；把 `%LOCALAPPDATA%\rdc\renderdoc` 放到 PATH 前面 |
| 想升级到新版 rdc-cli | 重跑流水线（换 `rdc-cli` 分支/版本）即可；payload 只在 RenderDoc 换版本时重建 |

---

## 8. 版本信息

| 组件 | 版本 | 备注 |
|---|---|---|
| rdc-cli | {{RDC_VERSION}} | 由流水线从 `BlurryLight/rdc-cli` 源码构建 |
| RenderDoc 模块 | {{RDOC_VERSION}} / commit `{{RDOC_COMMIT}}` | 由流水线从上游 tag 源码编译 |
| Python ABI | {{RDC_PYTHON}} | 与模块绑定，不可更换 |
| VC 运行库 | `msvcp140` / `vcruntime140` / `vcruntime140_1` | 随包分发，目标机无需装 VC++ redist |

> 版本对应关系以 `payload/win-x64/MANIFEST.json` 为准（含每个文件的 sha256）。
