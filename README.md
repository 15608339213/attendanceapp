# 西安石油大学就创中心内务系统 · 全流程手册

> 项目位置：`/home/ubuntu/attendance-app`（轻量应用服务器 120.53.31.101，实例 lhins-eem01puo，地域 ap-beijing）
> 访问地址：http://120.53.31.101:8000
> 代码仓库：https://github.com/15608339213/attendanceapp（main 分支）

## 一、项目概述

面向就创中心部门内部的考勤内务管理系统，覆盖成员注册登录、拍照打卡、假勤申请、值班管理、审批流转和考勤导出全流程。移动端优先的响应式网页应用，手机浏览器直接使用。

**技术栈**：
- 后端：Python 3.12 + FastAPI + uvicorn（虚拟环境 `venv/`）
- 前端：单页应用 `static/index.html`（原生 HTML/CSS/JS，无构建依赖）
- 数据库：SQLite（`attendance.db`）
- 照片存储：本地目录 `uploads/`

**关键文件**：

| 文件 | 说明 |
| --- | --- |
| main.py | 后端全部接口与业务逻辑 |
| static/index.html | 前端单页应用 |
| attendance.db | SQLite 数据库（含账号密码哈希，勿上传） |
| uploads/ | 打卡照片存储目录（勿上传） |
| attendance.log | 服务运行日志 |
| .gitignore | 忽略 venv/数据库/日志/照片 |

## 二、账号体系

| 角色 | 说明 |
| --- | --- |
| 管理员 | 用户名 jyb（姓名显示『管理员』），拥有管理端全部功能 |
| 普通成员 | 注册流程：填写班级+姓名+手机号 → 用户名为『班级+姓名』，手机号即登录密码 → 管理员审核通过后可登录 |
| 导入成员 | 值班表导入自动创建，状态为『未激活』，无法登录；管理员批量激活后初始密码 123456 |

**密码规则**：注册成员密码=手机号（11位校验）；导入/重置密码统一为 123456；存储采用 PBKDF2-SHA256 加盐哈希。

**登录鉴权**：自实现 HMAC 签名 Token（7天有效），请求头 `Authorization: Bearer <token>`。

## 三、功能清单

### 成员端（底部导航：打卡 / 假勤 / 我的）

1. **拍照打卡**：点击签到/签退按钮调起手机相机拍照（自动压缩至1080宽），照片提交后完成打卡；未拍照无法打卡（前后端双重校验）；定位信息可选附带
2. **假勤申请**：请假 / 销假 / 换班补勤（选择对象）/ 临时加班，统一提交管理员审批，可查看审批状态与历史
3. **个人中心**：近31天考勤记录、我的值班安排
4. **值班看板**：班次×星期表格展示全部门值班，支持切换周次
5. **值班查询**：输入姓名查询该成员所有值班班次和时间
6. **记住密码**：登录页勾选后账号密码保存在手机本地，自动填充

### 管理端（底部导航：管理，仅管理员可见，折叠卡片式界面）

1. **今日打卡**：当日打卡人数、姓名搜索（模糊匹配）、签到签退时间、值班班次、考勤状态
2. **审批中心**：待办列表、单条通过/驳回、一键全部通过/驳回
3. **成员管理**：复选框勾选批量操作（批量通过/激活/重置密码123456/批量删除）、全选、角色分配、单个停用/删除
4. **部门成员信息**：姓名、用户名、班级、角色、状态、加入时间总览
5. **值班表智能导入**：上传 docx/xlsx/csv 文件，自动识别表头星期、时间段和单元格姓名，批量生成值班安排（可设重复1~16周）；未注册姓名自动创建『未激活』成员
6. **值班安排管理**：手动添加/删除值班（全天/早班/晚班）
7. **打卡照片查看**：按日期查看成员签到/签退照片及对应值班班次
8. **考勤导出**：按月导出 CSV（出勤、迟到、早退、外勤、请假、加班、换班汇总+每日明细）
9. **系统配置**：签到中心坐标、允许半径、地图服务商与 API Key（高德/腾讯，用于中文地点显示）

## 四、考勤规则

- 上班时间 08:30，晚于此签到标记『迟到』；下班时间 17:30，早于此签退标记『早退』
- 打卡照片超出签到范围（若提交定位）标记『外勤』
- 每人每日一条记录（签到+签退），重复打卡被拒绝
- 规则常量在 main.py 顶部：`WORK_START` / `WORK_END`

## 五、数据库结构

| 表 | 字段要点 |
| --- | --- |
| users | id, username(唯一), name(唯一), password_hash, role(admin/member), status(active/pending/imported/rejected), class_name, created_at |
| records | user_id, date, check_in, check_out, in_lat/in_lng/in_range, out_lat/out_lng, photo_in, photo_out, status；唯一索引(user_id, date) |
| requests | user_id, type(leave/cancel_leave/shift/overtime), start_time, end_time, reason, target_user_id, status(pending/approved/rejected), reviewer_id, review_comment, reviewed_at |
| duty | user_id, date, shift, note；唯一约束(user_id, date, shift) |
| config | key/value：center_lat, center_lng, allow_range, map_provider, map_key |

## 六、API 接口速查

**认证**：`POST /api/auth/register`（班级+姓名+手机号）、`POST /api/auth/login`、`GET /api/auth/me`

**打卡**：`POST /api/clock-in`、`POST /api/clock-out`（body 含 photo 必填，lat/lng 可选）、`GET /api/today`、`GET /api/records`

**假勤**：`POST /api/requests`、`GET /api/requests/mine`、`GET /api/requests/pending`（管理员）、`POST /api/requests/{id}/review`、`POST /api/requests/review-all`

**值班**：`GET /api/duty`、`GET /api/duty/mine`、`GET /api/duty/board?start=`（看板）、`GET /api/duty/query?name=`（查询）、`POST /api/duty`、`DELETE /api/duty/{id}`、`POST /api/duty/import`（文件base64+开始日期+周数）

**成员管理（管理员）**：`GET /api/admin/users`、`POST /api/admin/users/{id}/status`、`POST /api/admin/users/{id}/role`、`DELETE /api/admin/users/{id}`、`POST /api/admin/users/approve-all`、`POST /api/admin/users/batch`（action: delete/reset_password/activate/approve + ids）

**其他**：`GET /api/admin/today-records?name=`、`GET /api/admin/photos?date=`、`GET /api/export?month=`、`GET/PUT /api/config`、`GET /api/geocode?lat=&lng=`、`GET /api/stats`

**静态资源**：`/`（前端页面）、`/uploads/`（打卡照片）

## 七、部署与运维

### 启动/重启服务

```bash
cd /home/ubuntu/attendance-app
pkill -9 -f 'uvicorn main:app'; sleep 2
nohup venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > attendance.log 2>&1 &
sleep 3 && ss -tlnp | grep 8000   # 确认监听
```

### 常用排查

```bash
tail -30 attendance.log                    # 查看报错
venv/bin/python -m py_compile main.py      # 语法检查
sqlite3 attendance.db 'SELECT ...'         # 数据核查（或用 venv/bin/python -c）
```

### 注意事项

- 防火墙已放行 TCP 8000（轻量服务器控制台）
- 修改 main.py 后必须重启服务才生效；修改 index.html 刷新浏览器即可
- 数据库结构变更通过 init_db() 中的 ALTER TABLE 自动迁移，新增列写在 rcols 循环里
- SECRET_KEY 在 main.py 顶部，修改后所有登录 Token 失效（需重新登录）
- 服务器重启后服务不会自启，需手动执行启动命令

### 代码更新到 GitHub

```bash
cd /home/ubuntu/attendance-app
git add -A && git commit -m '描述变更'
git push origin main
```

## 八、值班表导入格式要求

- 支持 docx / xlsx / csv；图片格式暂不支持（需先转文档）
- 表头需含星期列（一/二/…/日/天，支持『星期X』『周X』写法）
- 行首列为时间段（如 8:20~10:10），单元格为姓名（顿号/逗号分隔多人）
- 含『组长』的连写单元格自动跳过（如卫生值日表）
- 开始日期自动对齐到周一，按设定周数循环生成

## 九、已知限制与后续规划

1. 服务无开机自启（可配置 systemd）
2. 图片值班表不支持 OCR 识别
3. 中文地点显示需配置地图 API Key（高德/腾讯免费申请）
4. 无 HTTPS（浏览器定位/相机在非安全源下部分受限，建议后续配置域名+证书）
5. 可扩展方向：考勤日历视图、消息通知、数据看板图表、成员自助改密
