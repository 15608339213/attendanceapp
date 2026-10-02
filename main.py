from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import sqlite3, datetime, os, hashlib, hmac, base64, time, csv, io, json, re, zipfile
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET
from math import radians, sin, cos, asin, sqrt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'attendance.db')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
app = FastAPI(title='西安石油大学就创中心内务系统')

SECRET_KEY = 'xsyu-jczx-neiwu-secret-2026'
TOKEN_TTL = 7 * 86400
WORK_START = '08:30'
WORK_END = '17:30'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()
    return salt + '$' + h

def verify_pw(pw, stored):
    try:
        salt = stored.split('$', 1)[0]
    except Exception:
        return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)

def make_token(uid, role):
    payload = '%d.%s.%d' % (uid, role, int(time.time()) + TOKEN_TTL)
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(('%s.%s' % (payload, sig)).encode()).decode()

def parse_token(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit('.', 1)
        expect = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        uid, role, exp = payload.split('.')
        if int(exp) < time.time():
            return None
        return int(uid), role
    except Exception:
        return None

def get_config(conn):
    return {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM config')}

def haversine(lat1, lng1, lat2, lng2):
    rlat1, rlng1, rlat2, rlng2 = radians(lat1), radians(lng1), radians(lat2), radians(lng2)
    d = 2 * asin(sqrt(sin((rlat2 - rlat1) / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin((rlng2 - rlng1) / 2) ** 2))
    return d * 6371000

def now_str():
    return datetime.datetime.now().strftime('%H:%M:%S')

def today():
    return datetime.date.today().isoformat()

def now_dt():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def init_db():
    conn = get_db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        name TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        role TEXT DEFAULT 'member',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        check_in TEXT,
        check_out TEXT,
        in_lat REAL, in_lng REAL, in_range INTEGER,
        out_lat REAL, out_lng REAL,
        status TEXT DEFAULT '正常',
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        reason TEXT,
        target_user_id INTEGER,
        status TEXT DEFAULT 'pending',
        reviewer_id INTEGER,
        review_comment TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        reviewed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS duty (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        shift TEXT DEFAULT '全天',
        note TEXT,
        UNIQUE(user_id, date, shift)
    );
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_date ON records(user_id, date);
    ''')
    ucols = [r['name'] for r in conn.execute('PRAGMA table_info(users)')]
    for col, ddl in [('username', 'ALTER TABLE users ADD COLUMN username TEXT'),
                     ('password_hash', 'ALTER TABLE users ADD COLUMN password_hash TEXT'),
                     ('role', "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'member'"),
                     ('status', "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'"),
                     ('class_name', "ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")]:
        if col not in ucols:
            conn.execute(ddl)
    rcols = [r['name'] for r in conn.execute('PRAGMA table_info(records)')]
    for col, ddl in [('in_lat', 'ALTER TABLE records ADD COLUMN in_lat REAL'),
                     ('in_lng', 'ALTER TABLE records ADD COLUMN in_lng REAL'),
                     ('in_range', 'ALTER TABLE records ADD COLUMN in_range INTEGER'),
                     ('out_lat', 'ALTER TABLE records ADD COLUMN out_lat REAL'),
                     ('out_lng', 'ALTER TABLE records ADD COLUMN out_lng REAL'),
                     ('photo_in', 'ALTER TABLE records ADD COLUMN photo_in TEXT'),
                     ('photo_out', 'ALTER TABLE records ADD COLUMN photo_out TEXT')]:
        if col not in rcols:
            conn.execute(ddl)
    conn.execute("UPDATE users SET username=name WHERE username IS NULL OR username=''")
    if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        conn.execute('INSERT INTO users(username,name,password_hash,role,status) VALUES(?,?,?,?,?)',
                     ('admin', '管理员', hash_pw('admin123'), 'admin', 'active'))
    for k, v in [('center_lat', '34.2090'), ('center_lng', '108.9350'), ('allow_range', '500'),
                 ('map_provider', ''), ('map_key', '')]:
        conn.execute('INSERT OR IGNORE INTO config(key,value) VALUES(?,?)', (k, v))
    conn.commit()
    conn.close()

init_db()

UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)

def save_photo(data):
    if not data or not data.startswith('data:image/'):
        raise HTTPException(400, '请拍照后打卡（需要提交照片）')
    try:
        head, b64 = data.split(',', 1)
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, '图片数据无效')
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, '图片过大，请重新拍摄')
    ext = 'png' if 'png' in head else 'jpg'
    fname = '%d_%s.%s' % (int(time.time() * 1000), os.urandom(4).hex(), ext)
    with open(os.path.join(UPLOADS_DIR, fname), 'wb') as f:
        f.write(raw)
    return fname

def current_user(authorization: str = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, '未登录')
    parsed = parse_token(authorization[7:])
    if not parsed:
        raise HTTPException(401, '登录已过期，请重新登录')
    uid, role = parsed
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    if not user or user['status'] != 'active':
        raise HTTPException(401, '账号不可用')
    return user

def require_admin(user=Depends(current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, '需要管理员权限')
    return user

class RegisterIn(BaseModel):
    class_name: str
    name: str
    phone: str

class LoginIn(BaseModel):
    username: str
    password: str

class ClockBody(BaseModel):
    lat: float | None = None
    lng: float | None = None
    photo: str | None = None

class RequestIn(BaseModel):
    type: str
    start_time: str
    end_time: str | None = None
    reason: str = ''
    target_user_id: int | None = None

class ReviewIn(BaseModel):
    action: str
    comment: str = ''

class ReviewAllIn(BaseModel):
    action: str
    comment: str = ''

class DutyIn(BaseModel):
    user_id: int
    date: str
    shift: str = '全天'
    note: str = ''

class DutyImportIn(BaseModel):
    filename: str
    content_base64: str
    start_date: str
    weeks: int = 4

class StatusIn(BaseModel):
    status: str

class RoleIn(BaseModel):
    role: str

class ConfigIn(BaseModel):
    center_lat: float
    center_lng: float
    allow_range: int
    map_provider: str = ''
    map_key: str = ''

@app.post('/api/auth/register')
def register(r: RegisterIn):
    name = r.name.strip()
    cls = r.class_name.strip()
    if not name:
        raise HTTPException(400, '请输入姓名')
    if not re.match(r'^1\d{10}$', r.phone):
        raise HTTPException(400, '请输入正确的11位手机号（将作为登录密码）')
    username = cls + name
    if not username:
        raise HTTPException(400, '请输入班级和姓名')
    conn = get_db()
    try:
        conn.execute('INSERT INTO users(username,name,password_hash,role,status,class_name) VALUES(?,?,?,?,?,?)',
                     (username, name, hash_pw(r.phone), 'member', 'pending', cls))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, '该姓名或用户名已存在')
    conn.close()
    return {'message': '注册成功，请等待管理员审核后登录', 'username': username}

@app.post('/api/auth/login')
def login(l: LoginIn):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username=? OR name=?', (l.username, l.username)).fetchone()
    conn.close()
    if not user or not user['password_hash'] or not verify_pw(l.password, user['password_hash']):
        raise HTTPException(401, '用户名或密码错误')
    if user['status'] == 'imported':
        raise HTTPException(403, '账号未激活，请联系管理员激活（初始密码123456）')
    if user['status'] == 'pending':
        raise HTTPException(403, '注册待管理员审核，请稍后再试')
    if user['status'] != 'active':
        raise HTTPException(403, '账号已被停用')
    return {'token': make_token(user['id'], user['role']),
            'user': {'id': user['id'], 'name': user['name'], 'username': user['username'], 'role': user['role']}}

@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return {'id': user['id'], 'name': user['name'], 'username': user['username'], 'role': user['role']}

@app.get('/api/users')
def list_users(user=Depends(current_user)):
    conn = get_db()
    rows = conn.execute("SELECT id, name FROM users WHERE status='active' ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get('/api/admin/users')
def admin_users(user=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute('SELECT id, username, name, class_name, role, status, created_at FROM users ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post('/api/admin/users/approve-all')
def approve_all(user=Depends(require_admin)):
    conn = get_db()
    cur = conn.execute("UPDATE users SET status='active' WHERE status='pending'")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {'message': '已一键通过 %d 个待审核注册' % n, 'count': n}

class BatchIn(BaseModel):
    action: str
    ids: list

@app.post('/api/admin/users/batch')
def batch_users(b: BatchIn, user=Depends(require_admin)):
    if b.action not in ('delete', 'reset_password', 'activate', 'approve'):
        raise HTTPException(400, '无效的操作')
    ids = [i for i in b.ids if isinstance(i, int) and i != user['id']]
    if not ids:
        raise HTTPException(400, '未选择有效成员')
    ph = ','.join('?' * len(ids))
    conn = get_db()
    if b.action == 'delete':
        for uid in ids:
            conn.execute('DELETE FROM records WHERE user_id=?', (uid,))
            conn.execute('DELETE FROM requests WHERE user_id=?', (uid,))
            conn.execute('DELETE FROM duty WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM users WHERE id IN (%s)' % ph, ids)
        msg = '已批量删除 %d 个成员' % len(ids)
    elif b.action == 'reset_password':
        conn.execute('UPDATE users SET password_hash=? WHERE id IN (%s)' % ph, [hash_pw('123456')] + ids)
        msg = '已批量重置 %d 个成员密码为123456' % len(ids)
    elif b.action == 'activate':
        conn.execute("UPDATE users SET status='active', password_hash=? WHERE id IN (%s)" % ph,
                     [hash_pw('123456')] + ids)
        msg = '已批量激活 %d 个账号（初始密码123456）' % len(ids)
    else:
        conn.execute("UPDATE users SET status='active' WHERE id IN (%s)" % ph, ids)
        msg = '已批量通过 %d 个待审核注册' % len(ids)
    conn.commit()
    conn.close()
    return {'message': msg, 'count': len(ids)}

@app.post('/api/admin/users/{uid}/status')
def set_user_status(uid: int, s: StatusIn, user=Depends(require_admin)):
    if s.status not in ('active', 'pending', 'rejected'):
        raise HTTPException(400, '无效状态')
    conn = get_db()
    conn.execute('UPDATE users SET status=? WHERE id=?', (s.status, uid))
    conn.commit()
    conn.close()
    return {'message': '状态已更新'}

@app.post('/api/admin/users/{uid}/role')
def set_role(uid: int, r: RoleIn, user=Depends(require_admin)):
    if r.role not in ('admin', 'member'):
        raise HTTPException(400, '无效角色')
    conn = get_db()
    conn.execute('UPDATE users SET role=? WHERE id=?', (r.role, uid))
    conn.commit()
    conn.close()
    return {'message': '角色已更新'}

@app.delete('/api/admin/users/{uid}')
def delete_user(uid: int, user=Depends(require_admin)):
    if uid == user['id']:
        raise HTTPException(400, '不能删除自己')
    conn = get_db()
    conn.execute('DELETE FROM records WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM requests WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM duty WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit()
    conn.close()
    return {'message': '成员已删除'}

@app.get('/api/geocode')
def geocode(lat: float = Query(...), lng: float = Query(...), user=Depends(current_user)):
    conn = get_db()
    cfg = get_config(conn)
    conn.close()
    provider = cfg.get('map_provider', '')
    key = cfg.get('map_key', '')
    address = None
    try:
        if provider == 'amap' and key:
            url = 'https://restapi.amap.com/v3/geocode/regeo?key=%s&location=%s,%s' % (
                urllib.parse.quote(key), lng, lat)
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = json.loads(resp.read().decode())
            if data.get('status') == '1' and data.get('regeocode', {}).get('formatted_address'):
                address = data['regeocode']['formatted_address']
        elif provider == 'tencent' and key:
            url = 'https://apis.map.qq.com/ws/geocoder/v1/?location=%s,%s&key=%s' % (
                lat, lng, urllib.parse.quote(key))
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = json.loads(resp.read().decode())
            if data.get('status') == 0 and data.get('result', {}).get('address'):
                address = data['result']['address']
    except Exception:
        address = None
    return {'address': address, 'provider': provider if address else None}

@app.post('/api/clock-in')
def clock_in(c: ClockBody, user=Depends(current_user)):
    photo = save_photo(c.photo)
    conn = get_db()
    rec = conn.execute('SELECT * FROM records WHERE user_id=? AND date=?', (user['id'], today())).fetchone()
    if rec and rec['check_in']:
        conn.close()
        raise HTTPException(400, '今日已签到，无需重复打卡')
    t = now_str()
    status = '迟到' if t > WORK_START + ':00' else '正常'
    in_range = None
    if c.lat is not None and c.lng is not None:
        cfg = get_config(conn)
        dist = haversine(c.lat, c.lng, float(cfg['center_lat']), float(cfg['center_lng']))
        in_range = 1 if dist <= float(cfg['allow_range']) else 0
        if not in_range:
            status += '+外勤'
    if rec:
        conn.execute('UPDATE records SET check_in=?, status=?, in_lat=?, in_lng=?, in_range=?, photo_in=? WHERE id=?',
                     (t, status, c.lat, c.lng, in_range, photo, rec['id']))
    else:
        conn.execute('INSERT INTO records(user_id,date,check_in,status,in_lat,in_lng,in_range,photo_in) VALUES(?,?,?,?,?,?,?,?)',
                     (user['id'], today(), t, status, c.lat, c.lng, in_range, photo))
    conn.commit()
    conn.close()
    return {'message': '签到成功', 'time': t, 'status': status, 'in_range': in_range}

@app.post('/api/clock-out')
def clock_out(c: ClockBody, user=Depends(current_user)):
    photo = save_photo(c.photo)
    conn = get_db()
    rec = conn.execute('SELECT * FROM records WHERE user_id=? AND date=?', (user['id'], today())).fetchone()
    if not rec or not rec['check_in']:
        conn.close()
        raise HTTPException(400, '今日尚未签到，无法签退')
    if rec['check_out']:
        conn.close()
        raise HTTPException(400, '今日已签退，无需重复打卡')
    t = now_str()
    status = rec['status']
    if t < WORK_END + ':00':
        status = (status + '+早退') if status != '正常' else '早退'
    conn.execute('UPDATE records SET check_out=?, status=?, out_lat=?, out_lng=?, photo_out=? WHERE id=?',
                 (t, status, c.lat, c.lng, photo, rec['id']))
    conn.commit()
    conn.close()
    return {'message': '签退成功', 'time': t, 'status': status}

@app.get('/api/today')
def today_record(user=Depends(current_user)):
    conn = get_db()
    rec = conn.execute('SELECT * FROM records WHERE user_id=? AND date=?', (user['id'], today())).fetchone()
    conn.close()
    return dict(rec) if rec else None

@app.get('/api/records')
def my_records(month: str = Query(default=None), user=Depends(current_user)):
    conn = get_db()
    if month:
        rows = conn.execute('SELECT * FROM records WHERE user_id=? AND date LIKE ? ORDER BY date DESC',
                            (user['id'], month + '%')).fetchall()
    else:
        rows = conn.execute('SELECT * FROM records WHERE user_id=? ORDER BY date DESC LIMIT 31',
                            (user['id'],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post('/api/requests')
def create_request(r: RequestIn, user=Depends(current_user)):
    if r.type not in ('leave', 'cancel_leave', 'shift', 'overtime'):
        raise HTTPException(400, '无效的申请类型')
    if r.type == 'shift' and not r.target_user_id:
        raise HTTPException(400, '换班申请需选择换班对象')
    conn = get_db()
    cur = conn.execute('INSERT INTO requests(user_id,type,start_time,end_time,reason,target_user_id) VALUES(?,?,?,?,?,?)',
                       (user['id'], r.type, r.start_time, r.end_time, r.reason, r.target_user_id))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {'id': rid, 'message': '申请已提交，等待管理员审批'}

@app.get('/api/requests/mine')
def my_requests(user=Depends(current_user)):
    conn = get_db()
    rows = conn.execute('''SELECT r.*, t.name AS target_name FROM requests r
        LEFT JOIN users t ON t.id = r.target_user_id
        WHERE r.user_id=? ORDER BY r.id DESC''', (user['id'],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get('/api/requests/pending')
def pending_requests(user=Depends(require_admin)):
    conn = get_db()
    rows = conn.execute('''SELECT r.*, u.name AS user_name, t.name AS target_name
        FROM requests r JOIN users u ON u.id = r.user_id
        LEFT JOIN users t ON t.id = r.target_user_id
        WHERE r.status='pending' ORDER BY r.id DESC''').fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post('/api/requests/{rid}/review')
def review_request(rid: int, rv: ReviewIn, user=Depends(require_admin)):
    if rv.action not in ('approve', 'reject'):
        raise HTTPException(400, '无效的操作')
    conn = get_db()
    rec = conn.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
    if not rec:
        conn.close()
        raise HTTPException(404, '申请不存在')
    if rec['status'] != 'pending':
        conn.close()
        raise HTTPException(400, '该申请已处理')
    status = 'approved' if rv.action == 'approve' else 'rejected'
    conn.execute('UPDATE requests SET status=?, reviewer_id=?, review_comment=?, reviewed_at=? WHERE id=?',
                 (status, user['id'], rv.comment, now_dt(), rid))
    conn.commit()
    conn.close()
    return {'message': '审批完成', 'status': status}

@app.post('/api/requests/review-all')
def review_all(rv: ReviewAllIn, user=Depends(require_admin)):
    if rv.action not in ('approve', 'reject'):
        raise HTTPException(400, '无效的操作')
    status = 'approved' if rv.action == 'approve' else 'rejected'
    conn = get_db()
    cur = conn.execute("UPDATE requests SET status=?, reviewer_id=?, review_comment=?, reviewed_at=? WHERE status='pending'",
                       (status, user['id'], rv.comment, now_dt()))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {'message': '已一键%s %d 条申请' % ('通过' if rv.action == 'approve' else '驳回', n), 'count': n}

@app.get('/api/duty')
def list_duty(month: str = Query(default=None), user=Depends(current_user)):
    conn = get_db()
    if month:
        rows = conn.execute('''SELECT d.*, u.name FROM duty d JOIN users u ON u.id = d.user_id
            WHERE d.date LIKE ? ORDER BY d.date''', (month + '%',)).fetchall()
    else:
        rows = conn.execute('''SELECT d.*, u.name FROM duty d JOIN users u ON u.id = d.user_id
            ORDER BY d.date DESC LIMIT 100''').fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get('/api/duty/mine')
def my_duty(user=Depends(current_user)):
    conn = get_db()
    rows = conn.execute('SELECT * FROM duty WHERE user_id=? ORDER BY date DESC LIMIT 50', (user['id'],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post('/api/duty')
def add_duty(d: DutyIn, user=Depends(require_admin)):
    conn = get_db()
    try:
        conn.execute('INSERT INTO duty(user_id,date,shift,note) VALUES(?,?,?,?)',
                     (d.user_id, d.date, d.shift, d.note))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, '该成员当天此班次已存在')
    conn.close()
    return {'message': '值班安排已添加'}

@app.delete('/api/duty/{did}')
def del_duty(did: int, user=Depends(require_admin)):
    conn = get_db()
    conn.execute('DELETE FROM duty WHERE id=?', (did,))
    conn.commit()
    conn.close()
    return {'message': '值班安排已删除'}

@app.get('/api/duty/board')
def duty_board(start: str = Query(default=None), user=Depends(current_user)):
    if start:
        try:
            s = datetime.date.fromisoformat(start)
        except Exception:
            raise HTTPException(400, '日期格式错误')
    else:
        s = datetime.date.today()
    s = s - datetime.timedelta(days=s.weekday())
    dates = [(s + datetime.timedelta(days=i)).isoformat() for i in range(7)]
    conn = get_db()
    rows = conn.execute('''SELECT d.date, d.shift, u.name FROM duty d JOIN users u ON u.id = d.user_id
        WHERE d.date BETWEEN ? AND ? ORDER BY d.date, d.shift, u.id''', (dates[0], dates[-1])).fetchall()
    conn.close()
    board = {}
    for r in rows:
        board.setdefault(r['shift'], {}).setdefault(r['date'], []).append(r['name'])
    return {'start': dates[0], 'dates': dates, 'board': board}

@app.get('/api/duty/query')
def duty_query(name: str = Query(...), user=Depends(current_user)):
    conn = get_db()
    u = conn.execute('SELECT id FROM users WHERE name=?', (name,)).fetchone()
    if not u:
        conn.close()
        raise HTTPException(404, '成员不存在')
    rows = conn.execute('SELECT date, shift, note FROM duty WHERE user_id=? ORDER BY date', (u['id'],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

def docx_tables(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(z.read('word/document.xml'))
    tables = []
    for tbl in root.iter(W + 'tbl'):
        rows = []
        for tr in tbl.findall(W + 'tr'):
            cells = [''.join(t.text or '' for t in tc.iter(W + 't')).strip() for tc in tr.findall(W + 'tc')]
            rows.append(cells)
        tables.append(rows)
    return tables

def csv_tables(raw):
    text = raw.decode('utf-8-sig', errors='ignore')
    rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    return [rows]

def xlsx_tables(raw):
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(400, '服务器暂不支持 xlsx 解析，请将文件另存为 docx 或 csv 后上传')
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    tables = []
    for ws in wb.worksheets:
        rows = [[str(c).strip() if c is not None else '' for c in row] for row in ws.iter_rows(values_only=True)]
        tables.append(rows)
    return tables

def weekday_of(cell):
    cell = cell.strip()
    if not cell or len(cell) > 4:
        return None
    m = re.search(r'[星期周]\s*([一二三四五六日天])', cell)
    if m:
        ch = m.group(1)
    elif cell in ('一', '二', '三', '四', '五', '六', '日', '天', '七'):
        ch = '日' if cell == '七' else cell
    else:
        return None
    ch = '日' if ch in ('天', '七') else ch
    return '一二三四五六日'.index(ch) + 1

def parse_names(cell):
    return [n.strip() for n in re.split(r'[、，,;；\n]+', cell) if n.strip()]

@app.post('/api/duty/import')
def duty_import(d: DutyImportIn, user=Depends(require_admin)):
    try:
        raw = base64.b64decode(d.content_base64)
    except Exception:
        raise HTTPException(400, '文件内容解析失败')
    fname = (d.filename or '').lower()
    if fname.endswith('.docx'):
        tables = docx_tables(raw)
    elif fname.endswith('.csv'):
        tables = csv_tables(raw)
    elif fname.endswith('.xlsx'):
        tables = xlsx_tables(raw)
    elif fname.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif')):
        raise HTTPException(400, '暂不支持图片直接识别，请将值班表另存为 docx/xlsx/csv 后上传')
    else:
        raise HTTPException(400, '无法识别的文件格式，支持 docx/xlsx/csv')

    entries = []
    for tbl in tables:
        weekday_cols = None
        for row in tbl:
            cols = {}
            for ci, cell in enumerate(row):
                wd = weekday_of(cell)
                if wd is not None:
                    cols[ci] = wd
            if len(cols) >= 3:
                weekday_cols = cols
                continue
            if weekday_cols is None:
                continue
            time_slot = ''
            for ci, cell in enumerate(row):
                if ci in weekday_cols or not cell:
                    continue
                if re.search(r'\d{1,2}[:：]\d{2}', cell) or '~' in cell or '～' in cell:
                    time_slot = cell
                    break
            slot = time_slot or '值班'
            for ci, cell in enumerate(row):
                if ci in weekday_cols and cell:
                    if '组长' in cell:
                        continue
                    names = parse_names(cell)
                    if names:
                        entries.append((weekday_cols[ci], slot, names))
    if not entries:
        raise HTTPException(400, '未识别到值班表内容（需要表头含周一~周日，单元格为姓名）')

    try:
        start = datetime.date.fromisoformat(d.start_date)
    except Exception:
        raise HTTPException(400, '请提供正确的开始日期')
    start = start - datetime.timedelta(days=start.weekday())
    weeks = max(1, min(d.weeks, 20))

    conn = get_db()
    created_users = []
    skipped = []
    inserted = 0
    for wd, slot, names in entries:
        for name in names:
            clean = re.sub(r'^(组长|副组长|负责人)[:：]?\s*', '', name).strip()
            if not clean:
                continue
            u = conn.execute('SELECT id FROM users WHERE name=?', (clean,)).fetchone()
            if not u:
                if len(clean) > 4 and '·' not in clean:
                    if clean not in skipped:
                        skipped.append(clean)
                    continue
                cur = conn.execute("INSERT INTO users(username,name,password_hash,role,status,class_name) VALUES(?,?,?,?,?,?)",
                                   (clean, clean, hash_pw('123456'), 'member', 'imported', ''))
                uid = cur.lastrowid
                created_users.append(clean)
            else:
                uid = u['id']
            for w in range(weeks):
                date = (start + datetime.timedelta(days=w * 7 + wd - 1)).isoformat()
                try:
                    conn.execute('INSERT INTO duty(user_id,date,shift,note) VALUES(?,?,?,?)',
                                 (uid, date, slot, '导入自 ' + os.path.basename(d.filename)))
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
    conn.commit()
    conn.close()
    return {'message': '值班表导入完成', 'inserted': inserted, 'weeks': weeks,
            'start_date': start.isoformat(), 'created_users': created_users, 'skipped': skipped}

@app.get('/api/stats')
def stats(month: str = Query(default=None), user=Depends(current_user)):
    conn = get_db()
    if month:
        rows = conn.execute('''SELECT u.name, COUNT(r.id) AS days,
            SUM(CASE WHEN r.status LIKE '%迟到%' THEN 1 ELSE 0 END) AS late,
            SUM(CASE WHEN r.status LIKE '%早退%' THEN 1 ELSE 0 END) AS early,
            SUM(CASE WHEN r.in_range=0 THEN 1 ELSE 0 END) AS field
            FROM users u LEFT JOIN records r ON u.id=r.user_id AND r.date LIKE ?
            WHERE u.status='active' GROUP BY u.id ORDER BY u.id''', (month + '%',)).fetchall()
    else:
        rows = conn.execute('''SELECT u.name, COUNT(r.id) AS days,
            SUM(CASE WHEN r.status LIKE '%迟到%' THEN 1 ELSE 0 END) AS late,
            SUM(CASE WHEN r.status LIKE '%早退%' THEN 1 ELSE 0 END) AS early,
            SUM(CASE WHEN r.in_range=0 THEN 1 ELSE 0 END) AS field
            FROM users u LEFT JOIN records r ON u.id=r.user_id
            WHERE u.status='active' GROUP BY u.id ORDER BY u.id''').fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get('/api/export')
def export(month: str = Query(default=None), user=Depends(require_admin)):
    m = month or datetime.date.today().strftime('%Y-%m')
    prefix = m + '%'
    conn = get_db()
    users = conn.execute("SELECT * FROM users WHERE status='active' ORDER BY id").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['西安石油大学就创中心内务系统 - 考勤导出'])
    w.writerow(['统计月份', m])
    w.writerow([])
    w.writerow(['姓名', '出勤天数', '迟到', '早退', '外勤', '请假天数', '加班次数', '换班次数'])
    for u in users:
        recs = conn.execute('SELECT * FROM records WHERE user_id=? AND date LIKE ? ORDER BY date',
                            (u['id'], prefix)).fetchall()
        late = sum(1 for r in recs if '迟到' in (r['status'] or ''))
        early = sum(1 for r in recs if '早退' in (r['status'] or ''))
        field = sum(1 for r in recs if r['in_range'] == 0)
        leave_days = 0
        for q in conn.execute("SELECT start_time, end_time FROM requests WHERE user_id=? AND type='leave' AND status='approved' AND start_time LIKE ?",
                              (u['id'], prefix)).fetchall():
            try:
                d1 = datetime.date.fromisoformat(q['start_time'][:10])
                d2 = datetime.date.fromisoformat((q['end_time'] or q['start_time'])[:10])
                leave_days += max((d2 - d1).days + 1, 0)
            except Exception:
                pass
        ot = conn.execute("SELECT COUNT(*) c FROM requests WHERE user_id=? AND type='overtime' AND status='approved' AND start_time LIKE ?",
                          (u['id'], prefix)).fetchone()['c']
        sh = conn.execute("SELECT COUNT(*) c FROM requests WHERE user_id=? AND type='shift' AND status='approved' AND start_time LIKE ?",
                          (u['id'], prefix)).fetchone()['c']
        w.writerow([u['name'], len(recs), late, early, field, leave_days, ot, sh])
    w.writerow([])
    w.writerow(['明细：姓名', '日期', '签到', '签退', '签到定位', '状态'])
    for u in users:
        for r in conn.execute('SELECT * FROM records WHERE user_id=? AND date LIKE ? ORDER BY date',
                              (u['id'], prefix)).fetchall():
            loc = '范围内' if r['in_range'] == 1 else ('范围外' if r['in_range'] == 0 else '未定位')
            w.writerow([u['name'], r['date'], r['check_in'] or '', r['check_out'] or '', loc, r['status']])
    conn.close()
    data = '\ufeff' + buf.getvalue()
    return StreamingResponse(iter([data]), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename=attendance_%s.csv' % m})

@app.get('/api/config')
def read_config(user=Depends(require_admin)):
    conn = get_db()
    cfg = get_config(conn)
    conn.close()
    return cfg

@app.put('/api/config')
def write_config(c: ConfigIn, user=Depends(require_admin)):
    conn = get_db()
    for k, v in [('center_lat', str(c.center_lat)), ('center_lng', str(c.center_lng)),
                 ('allow_range', str(c.allow_range)), ('map_provider', c.map_provider or ''),
                 ('map_key', c.map_key or '')]:
        conn.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
    conn.commit()
    conn.close()
    return {'message': '配置已保存'}

@app.get('/api/admin/today-records')
def admin_today_records(name: str = Query(default=None), user=Depends(require_admin)):
    conn = get_db()
    if name:
        rows = conn.execute('''SELECT r.*, u.name AS user_name,
            (SELECT GROUP_CONCAT(d2.shift) FROM duty d2 WHERE d2.user_id=u.id AND d2.date=r.date) AS shifts
            FROM records r JOIN users u ON u.id=r.user_id
            WHERE r.date=? AND u.name LIKE ? ORDER BY r.check_in''',
            (today(), '%' + name + '%')).fetchall()
    else:
        rows = conn.execute('''SELECT r.*, u.name AS user_name,
            (SELECT GROUP_CONCAT(d2.shift) FROM duty d2 WHERE d2.user_id=u.id AND d2.date=r.date) AS shifts
            FROM records r JOIN users u ON u.id=r.user_id
            WHERE r.date=? ORDER BY r.check_in''', (today(),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get('/api/admin/photos')
def admin_photos(date: str = Query(default=None), user=Depends(require_admin)):
    d = date or today()
    conn = get_db()
    rows = conn.execute('''SELECT r.date, r.check_in, r.check_out, r.photo_in, r.photo_out, r.status, u.name,
        (SELECT GROUP_CONCAT(d2.shift) FROM duty d2 WHERE d2.user_id=u.id AND d2.date=r.date) AS shifts
        FROM records r JOIN users u ON u.id=r.user_id
        WHERE r.date=? AND (r.photo_in IS NOT NULL OR r.photo_out IS NOT NULL)
        ORDER BY r.check_in''', (d,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

app.mount('/uploads', StaticFiles(directory=UPLOADS_DIR), name='uploads')
app.mount('/', StaticFiles(directory=STATIC_DIR, html=True), name='static')
