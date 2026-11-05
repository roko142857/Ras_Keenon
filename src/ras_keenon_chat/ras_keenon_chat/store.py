"""SQLite conversation and receipt persistence. API credentials are never stored here."""
import json
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self,path):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(str(path),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,created REAL,updated REAL);
        CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,session_id TEXT,state TEXT,created REAL,finished REAL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT,turn_id TEXT,role TEXT,content TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,session_id TEXT,turn_id TEXT,kind TEXT,state TEXT,title TEXT,data TEXT,created REAL,updated REAL);
        CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT);
        ''')
        self.db.commit()
        # Recover history without replaying any model request or robot action.
        for row in self.db.execute("SELECT * FROM turns WHERE state='running'").fetchall():
            with self.lock,self.db:
                self.db.execute("UPDATE receipts SET state='interrupted',updated=? WHERE turn_id=? AND state='running'",(time.time(),row['id']))
            self.finish_turn(row['id'],'interrupted')
            self.message(row['session_id'],row['id'],'assistant','服务已重启，本轮对话已中断；不会自动重发动作。')

    def create_session(self):
        identity=str(uuid.uuid4());now=time.time()
        with self.lock,self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(identity,'新对话',now,now))
        return self.session(identity)

    def sessions(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT s.*,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id AND t.state='running') AS busy
                FROM sessions s ORDER BY updated DESC''')]

    def session(self,identity):
        with self.lock:
            row=self.db.execute('SELECT * FROM sessions WHERE id=?',(identity,)).fetchone()
            if not row:raise KeyError('unknown session')
            value=dict(row)
            value['messages']=[dict(r) for r in self.db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id',(identity,))]
            value['turns']=[dict(r) for r in self.db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY created',(identity,))]
            value['receipts']=[]
            for r in self.db.execute('SELECT * FROM receipts WHERE session_id=? ORDER BY created',(identity,)):
                item=dict(r);item['data']=json.loads(item['data']);value['receipts'].append(item)
            return value

    def new_turn(self,session_id,text):
        with self.lock,self.db:
            self.session(session_id)
            if self.db.execute("SELECT 1 FROM turns WHERE session_id=? AND state='running'",(session_id,)).fetchone():
                raise RuntimeError('当前对话仍在处理中，请先取消本轮或等待完成。')
            identity=str(uuid.uuid4());now=time.time()
            self.db.execute('INSERT INTO turns VALUES(?,?,?,?,NULL)',(identity,session_id,'running',now))
            self.db.execute("UPDATE sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated=? WHERE id=?",
                            (text[:28],now,session_id))
            self.message(session_id,identity,'user',text)
            return identity

    def message(self,session_id,turn_id,role,content):
        with self.lock,self.db:
            now=time.time()
            self.db.execute('INSERT INTO messages(session_id,turn_id,role,content,created) VALUES(?,?,?,?,?)',
                            (session_id,turn_id,role,content,now))
            self.db.execute('UPDATE sessions SET updated=? WHERE id=?',(now,session_id))

    def finish_turn(self,identity,state):
        with self.lock,self.db:
            self.db.execute("UPDATE turns SET state=?,finished=? WHERE id=? AND state='running'",(state,time.time(),identity))

    def receipt(self,session_id,turn_id,kind,state,title,data,identity=None):
        identity=identity or str(uuid.uuid4());now=time.time()
        with self.lock,self.db:
            self.db.execute('''INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state,title=excluded.title,data=excluded.data,updated=excluded.updated''',
                (identity,session_id,turn_id,kind,state,title,json.dumps(data,ensure_ascii=False,allow_nan=False),now,now))
        return identity

    def save_settings(self,value):
        # Explicit allowlist prevents accidentally persisting credentials.
        safe={k:value[k] for k in ('mode','base_url','model','action_url') if k in value}
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('config',json.dumps(safe)))

    def load_settings(self):
        with self.lock:
            row=self.db.execute("SELECT value FROM settings WHERE name='config'").fetchone()
            return json.loads(row[0]) if row else {}

    def close(self):
        with self.lock:self.db.close()
