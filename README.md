# 孩子健康记录

一个适合家庭使用的健康记录 Web 应用。可以记录孩子的体温、症状、用药、就医与护理情况，并按时间查看、修改。网站支持添加到手机桌面，以独立窗口运行。

后端使用 Python、Flask、Gunicorn 和 SQLite，前端使用 HTML、CSS 与少量 JavaScript。应用使用账号密码、服务端可撤销会话、CSRF 防护和 HTTPS 反向代理；同一部署中的账号共享全部家庭记录。

## 功能

- 记录和编辑病程，按时间分页查看；并发编辑时提示冲突。
- 登录过期时保留当前页面上尚未保存的输入，重新登录后可继续保存。
- 支持安装为 Web 应用。健康记录需要联网访问；离线只显示联网提示，不在 Service Worker 中缓存记录或表单。
- 使用 SQLite 在线备份，校验快照完整性；备份任务不启动已停止的应用。

## 本地运行

需要 Python 3.12 或更新版本。以下命令只创建本机测试数据，首次生成密钥时不会覆盖已有密钥：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
export HEALTH_DATA="$PWD/data"
mkdir -p "$HEALTH_DATA"
chmod 700 "$HEALTH_DATA"
.venv/bin/python -c 'import os; p=os.path.join(os.environ["HEALTH_DATA"],"session.key"); fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,os.urandom(32)); os.close(fd)'
.venv/bin/python app.py set-password admin
.venv/bin/gunicorn --bind 127.0.0.1:18765 'app:create_app()'
```

访问 `http://localhost:18765/`。应用设置了 Secure Cookie；如果浏览器不接受 localhost 的安全 Cookie，使用本地 HTTPS。`data/` 和数据库文件不应提交到版本库。

## 配置与部署

- `HEALTH_DATA`：运行数据目录；默认 `/var/lib/child-health`。目录中包含数据库和 `session.key`，必须限制文件权限，并妥善备份密钥。
- `HEALTH_TRUSTED_HOSTS`：逗号分隔的允许访问的主机名，默认只允许 `localhost,127.0.0.1`。公开部署必须显式设置自己的域名，例如 `HEALTH_TRUSTED_HOSTS=journal.example.org`。不要填 URL、协议或通配的全部主机。
- 生产环境应使用 HTTPS，Gunicorn 只监听本机回环地址；示例 systemd 单元可从 `/etc/child-health/child-health.env` 读取环境变量。示例服务路径、系统账号和备份目录需要在部署前创建并核对。
- 当前反向代理设置只信任**一层**会设置 `X-Forwarded-For` 和 `X-Forwarded-Proto` 的可信代理。部署拓扑变化时，需同步调整 `ProxyFix` 配置，禁止直接暴露后端端口。
- 记录时间使用 `Australia/Melbourne` 本地时间；如果用于其他时区，需要在应用、备份脚本、定时器和页面文案中同步调整。夏令时切换期间的本地时刻可能有歧义。
- 备份脚本默认读取 `/var/lib/child-health/health.sqlite3`，保存至 `/data/child-health-backups`。这些路径也出现在 systemd 单元中，调整时需一起修改。备份默认保留在同一台机器上，建议自行设置加密异地备份及监控。

本公开备份移除了原部署的专用域名；`HEALTH_TRUSTED_HOSTS` 是为公开版本增加的配置项。现有生产部署不因这个仓库而自动改变。

## 测试

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
node tests/test_service_worker.cjs
```

测试使用临时 SQLite 数据库及合成账号，不需要生产数据。Service Worker 测试验证网络请求处理、离线提示与不使用 Cache Storage。
