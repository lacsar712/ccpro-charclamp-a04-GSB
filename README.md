# CharClamp-01 · 炭窑焖烧志

窑场炭窑与焖烧班次台账基线项目（Litestar + SQLAlchemy 2 + Jinja2 + HTMX）。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web | Litestar · Jinja2 · HTMX CDN · Session 认证 |
| 数据 | SQLAlchemy 2（async） · PostgreSQL 15 |
| 部署 | Docker Compose · Uvicorn |
| 结构 | `domain/` · `infra/` · `web/` 分层（非 Django apps） |

## 路径与端口

- **项目路径**：`d:\work\document\bytecode\claudeCodePro\CharClamp\CharClamp-01`
- **Web**：http://localhost:4750
- **PostgreSQL**：localhost:6150

## 演示账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `123456` | 管理员 |
| `worker` | `123456` | 操作工 |

登录页已预填 `admin` / `123456`。entrypoint 会建表并写入种子数据（窑场 **乌石岗焖烧坞**，窑号如 **坞东-甲 / 坞东-乙 / 河沿-丙**）。

## 主界面：焖烧时间轴

登录后进入全宽 **焖烧时间轴**（不再使用侧栏 + 双 CRUD 列表）：

1. **顶部窑剪影行**：每座炭窑以 SVG 剪影展示；点击某窑用 HTMX 局部刷新下方时间轴，并更新地址栏 `?clamp_id=`；「全部窑」取消筛选。
2. **纵向时间轴**：按开始时间倒序列出 `BurnShift`；每条卡片带窑号徽章（再点可开抽屉）、峰值温度、炭品与当前窑态。
3. **侧抽屉（非独立编辑页）**：「登记班次」写入新班次；点窑徽章打开操作抽屉，可标记「已出炭」（受峰值规则约束）。

## 业务规则

炭窑状态不可设为「已出炭」（`drawn`），除非该窑**最近一条** `BurnShift` 的 `peakTempC` 已记录且 **≥ 400℃**。

规则实现：`src/charclamp/domain/rules.py`

## 雨棚停火令

雨棚停火令生效期间，该窑场**禁止再登记任何焖烧班次，已有时间轴只读**。顶栏「雨棚停火令」进入专页（含现行列表、挂令、收令），顶栏「焖烧时间轴」随时可切回时间轴。

**令字段**（`CeasefireOrder`）：窑场、生效时刻、计划收令日、降雨摘要、挂令人、收令时刻（可空，收令时写入）。

**权限与唯一性**：

- 仅管理员（`role=admin`）可挂令 `POST /ceasefire/issue` 与收令 `POST /ceasefire/{id}/lift`；非管理员调用不产生写入。
- 同一窑场存在未收令时不可挂第二条；数据库对 `site_id WHERE lifted_at IS NULL` 建部分唯一索引，两名管理员并发挂令也只落一条（另一条收到中文提示）。
- 种子数据对「乌石岗焖烧坞」挂了一条未收令（幂等：收令后重启不会复活）。

**拦截范围**：

| 操作 | 停火令生效时 |
| --- | --- |
| 抽屉「登记班次」（选窑提示 + 禁提交） | 拦截，中文说明「雨棚停火」 |
| 直打保存接口 `POST /shifts/new` | 服务端硬拦截，不依赖前端藏按钮 |
| 已有焖烧时间轴 | 只读展示，顶部有停火横幅 |
| 标记已出炭 / 改回已码窑 `POST /clamps/{id}/status` | **不拦截**，仍走原峰值规则（最近班次峰值 ≥ 400℃） |

停火期的窑剪影带雨棚角标与「停火」字样。
`

## 快速启动

```bash
cd d:\work\document\bytecode\claudeCodePro\CharClamp\CharClamp-01
docker compose up --build
```

浏览器打开 http://localhost:4750

## 目录结构

```
CharClamp-01/
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
└── src/charclamp/
    ├── main.py
    ├── domain/          # models + rules
    ├── infra/           # db + seed + security
    └── web/             # controllers + templates + static
```
