# 配置方案（推荐）

本项目的配置分为三层：

1) **环境变量（.env）**：部署级配置（重启生效），建议只放密钥与部署相关项。  
2) **系统设置（WebUI）**：业务级配置（热更新），保存到 `data/settings.yaml`（或数据库）。  
3) **账号池（WebUI）**：账号数据，保存到 `data/accounts.json`（或数据库）。  

---

## 1) 环境变量（.env）

复制 `.env.example` 为 `.env`，至少设置：

- `ADMIN_KEY`（必填）：管理面板登录密钥。

推荐额外设置：

- `SESSION_SECRET_KEY`（推荐）：Session 签名密钥。固定后可避免重启导致登录态失效。
- `PATH_PREFIX`（推荐）：将整个应用挂载到 `/<PATH_PREFIX>/*` 下（WebUI、/admin、/v1、/public 等都会在该前缀下可用）。
  - 典型用途：反代只放行前缀路径，减少暴露面。
  - 访问方式：`https://host/<PATH_PREFIX>/`
  - 反向代理建议（Nginx 示例，**保留前缀**，由应用内部剥离）：
    ```nginx
    location = /<PATH_PREFIX> { return 308 /<PATH_PREFIX>/; }
    location /<PATH_PREFIX>/ {
      proxy_pass http://127.0.0.1:7860;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    ```

可选：

- `PORT`：监听端口（默认 `7860`）。
- `DATABASE_URL`：PostgreSQL 连接串（可选，用于持久化账户/设置/统计）。
- `FRONTEND_ORIGIN` / `ALLOW_ALL_ORIGINS`：仅在 WebUI 与后端跨域时需要配置。

不推荐：

- `ACCOUNTS_CONFIG`：会覆盖 `accounts.json/数据库`，容易导致 WebUI 修改“看起来不生效”。

---

## 2) 系统设置（WebUI / data/settings.yaml）

进入管理面板后，在「系统设置」里配置业务参数（例如 API Key、代理、自动注册/刷新、节点池等）。  
保存后会写入 `data/settings.yaml` 并**热更新**。

说明：
- `ADMIN_KEY/SESSION_SECRET_KEY/PATH_PREFIX` 等部署级变量不在 WebUI 中修改。
- 若启用数据库存储，系统设置会持久化在数据库中（文件作为降级/本地模式）。

---

## 3) 账号池（WebUI / data/accounts.json）

账号的导入、禁用、删除、自动补号等均通过 WebUI 操作。  
数据默认存放在 `data/accounts.json`，启用数据库后会持久化在数据库中。
