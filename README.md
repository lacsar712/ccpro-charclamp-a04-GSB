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

## 雨棚停火令（Ceasefire）

降雨期间可对**整个窑场**挂「雨棚停火令」。顶栏可点开 **焖烧时间轴** 与 **雨棚停火令** 两个入口。

令字段：窑场、生效时刻、计划收令日（可空）、降雨摘要、挂令人、收令时刻（可空，收令时写入，并记录收令人）。

- **仅管理员**可挂令与收令（后端强制校验，非管理员直打接口也会被中文回绝）。
- **同一窑场未收令时不可再挂第二条**：应用层预检 + 数据库「未收令」部分唯一索引
  （`uq_ceasefire_open_per_site ... WHERE lifted_at IS NULL`）双保险；两名管理员并发挂令只落一条，败者得到中文提示，登录态与时间轴不受影响。
- **拦截范围**：
  - 停火令生效中，该窑场**禁止再登记任何焖烧班次**——侧抽屉登记表单会显示中文停火说明并禁用提交，直打 `POST /shifts/new` 同样被后端以中文理由拦截（**不是只藏按钮**）。
  - 已有时间轴只读（不再写入新班次，既有记录照常查看）。
  - **不受影响**：「标记已出炭」与「改回已码窑」不走停火令校验，仍只走原峰值规则（≥ 400℃）。
- 停火期内窑剪影加 **雨棚角标**，时间轴顶部显示停火横幅；收令后角标消失、可重新登记班次。
- 种子数据默认挂**一条未收令**（窑场 乌石岗焖烧坞），便于直接查看拦截效果。

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
