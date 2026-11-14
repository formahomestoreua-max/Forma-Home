"""Customer accounts, orders and loyalty points for LOCAL classroom demonstration.
All prices, balances, statuses and rewards are calculated on the server.
Not an audited production payment or fulfillment backend.
"""
import base64, hashlib, hmac, json, os, re, secrets, sqlite3, time, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs
import mailer
import bank_transfer

ROOT = Path(__file__).resolve().parent
DB = ROOT / '.forma_clients.sqlite3'
_guard = Lock()
_attempts = {}
DEFAULT_SETTINGS={'own_standard':290,'own_large':690,'own_priority':490,'assembly_cost':599,'bonus_rate':2,'bonus_rate_loyal':3,'bonus_regular_after':3,'bonus_limit':20,'free_standard_over':25000}
LIQPAY_PUBLIC = os.environ.get('FORMA_LIQPAY_PUBLIC','')
LIQPAY_PRIVATE = os.environ.get('FORMA_LIQPAY_PRIVATE','')
PUBLIC_BASE = os.environ.get('FORMA_PUBLIC_HTTPS_URL','').rstrip('/')
LIQPAY_READY = False  # Card checkout disabled; manual IBAN only

def connect():
    db=sqlite3.connect(DB,timeout=15)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=15000')
    return db

def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS contact_requests(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
           email TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'new', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
           email TEXT NOT NULL UNIQUE, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
           bonus INTEGER NOT NULL DEFAULT 0 CHECK(bonus>=0), created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS customer_sessions(token_hash TEXT PRIMARY KEY,customer_id INTEGER NOT NULL,
           csrf TEXT NOT NULL,expires INTEGER NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id));
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,customer_id INTEGER NOT NULL,
           created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', payment TEXT NOT NULL,
           payment_status TEXT NOT NULL DEFAULT 'unpaid', delivery TEXT NOT NULL,
           receiver TEXT NOT NULL, phone TEXT NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL,
           assembly INTEGER NOT NULL, subtotal INTEGER NOT NULL, delivery_cost INTEGER NOT NULL,
           assembly_cost INTEGER NOT NULL, bonus_used INTEGER NOT NULL, total INTEGER NOT NULL,
           bonus_earned INTEGER NOT NULL DEFAULT 0, comment TEXT NOT NULL DEFAULT '',
           FOREIGN KEY(customer_id) REFERENCES customers(id));
        CREATE TABLE IF NOT EXISTS order_items(order_id TEXT NOT NULL,product_id INTEGER NOT NULL,
           title TEXT NOT NULL,quantity INTEGER NOT NULL,unit_price INTEGER NOT NULL,color_choice TEXT NOT NULL DEFAULT '',
           FOREIGN KEY(order_id) REFERENCES orders(id));
        CREATE TABLE IF NOT EXISTS loyalty_events(id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL,
           order_id TEXT NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS product_reviews(id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL,product_id INTEGER NOT NULL,
           stars INTEGER NOT NULL CHECK(stars>=1 AND stars<=5),body TEXT NOT NULL,created_at INTEGER NOT NULL,
           UNIQUE(customer_id,product_id),FOREIGN KEY(customer_id) REFERENCES customers(id));
        CREATE TABLE IF NOT EXISTS product_questions(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,product_id INTEGER NOT NULL,body TEXT NOT NULL,reply TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id));
        CREATE TABLE IF NOT EXISTS custom_messages(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,
           sender TEXT NOT NULL CHECK(sender IN ('customer','manager')),body TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS custom_requests(id TEXT PRIMARY KEY,customer_id INTEGER NOT NULL,
           created_at INTEGER NOT NULL,category TEXT NOT NULL,phone TEXT NOT NULL,
           dimensions TEXT NOT NULL,budget INTEGER,details TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'new',
           FOREIGN KEY(customer_id) REFERENCES customers(id));
        ''')
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS commerce_settings (name TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS email_queue (
            id INTEGER PRIMARY KEY,order_id TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'order',
            to_email TEXT NOT NULL,subject TEXT NOT NULL,body_text TEXT NOT NULL,
            body_html TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,sent_at INTEGER,attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS payment_confirmations (
            order_id TEXT PRIMARY KEY REFERENCES orders(id),
            amount INTEGER NOT NULL,
            bank_reference TEXT NOT NULL,
            confirmed_at INTEGER NOT NULL,
            method TEXT NOT NULL DEFAULT 'manual_bank_verification');
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,status TEXT NOT NULL,
            delivery_date TEXT NOT NULL DEFAULT '',delivery_window TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL);
        """)
        columns={x['name'] for x in db.execute('PRAGMA table_info(orders)').fetchall()}
        for name in ('delivery_date','delivery_window','manager_note'):
            if name not in columns:db.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        item_cols={row['name'] for row in db.execute('PRAGMA table_info(order_items)')}
        if 'color_choice' not in item_cols:
            db.execute("ALTER TABLE order_items ADD COLUMN color_choice TEXT NOT NULL DEFAULT ''")
        # One-time migration: keep independently customized shop settings unchanged.
        was_migrated=db.execute("SELECT 1 FROM commerce_settings WHERE name='fh18_bonus_policy_v1'").fetchone()
        if not was_migrated:
            old={r['name']:r['value'] for r in db.execute('SELECT name,value FROM commerce_settings')}
            if old.get('bonus_rate')==5:
                db.execute("UPDATE commerce_settings SET value=2 WHERE name='bonus_rate'")
            if old.get('bonus_limit')==30:
                db.execute("UPDATE commerce_settings SET value=20 WHERE name='bonus_limit'")
            db.execute("INSERT INTO commerce_settings(name,value) VALUES('fh18_bonus_policy_v1',1)")
        for name,value in DEFAULT_SETTINGS.items():
            db.execute('INSERT OR IGNORE INTO commerce_settings(name,value) VALUES (?,?)',(name,value))
    with connect() as db:
        cols={row['name'] for row in db.execute('PRAGMA table_info(product_reviews)').fetchall()}
        if 'reply' not in cols:db.execute("ALTER TABLE product_reviews ADD COLUMN reply TEXT NOT NULL DEFAULT ''")
    try: os.chmod(DB,0o600)
    except OSError: pass

def settings(db=None):
    if db is not None:
        return {**DEFAULT_SETTINGS,**{x['name']:x['value'] for x in db.execute('SELECT name,value FROM commerce_settings')}}
    with connect() as temp:
        return settings(temp)

def freight(subtotal,delivery,config):
    if delivery=='own_standard' and config['free_standard_over']>0 and subtotal>=config['free_standard_over']:
        return 0
    return config[delivery]


def pwd_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),320_000).hex()

def user_session(handler):
    cookie=handler.headers.get('Cookie','')
    token=''
    for pair in cookie.split(';'):
        key,sep,value=pair.strip().partition('=')
        if sep and key=='FHCUST': token=value
    if not token: return None
    th=hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row=db.execute('SELECT c.id,c.name,c.email,c.bonus,s.csrf,s.expires FROM customer_sessions s JOIN customers c ON s.customer_id=c.id WHERE s.token_hash=?',(th,)).fetchone()
    return row if row and row['expires']>int(time.time()) else None

def cookie_line(token,age):
    secure='; Secure' if PUBLIC_BASE.startswith('https://') else ''
    return f'FHCUST={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}'

def fail(h,code,msg): return h.json_response(code,{'error':msg})

def require_user(h,write=False):
    s=user_session(h)
    if not s:
        fail(h,401,'Спочатку увійдіть в акаунт')
        return None
    if write and not hmac.compare_digest(s['csrf'],h.headers.get('X-FH-CUSTOMER-CSRF','')):
        fail(h,403,'Сесія змінилася. Оновіть сторінку.')
        return None
    return s

def get(h,path):
    if path=='/api/commerce/settings':
        return h.json_response(200,{'settings':settings(),'email':mailer.FROM_ADDRESS,'onlinePaymentsEnabled':False})
    if path=='/api/admin/mail':
        if not h.session():return fail(h,401,'Потрібно увійти')
        data=mailer.status(connect)
        data['settings']=settings()
        return h.json_response(200,data)
    if path.startswith('/api/reviews/'):
        try:pid=int(path.rsplit('/',1)[-1]);assert pid>0
        except (ValueError,AssertionError):return fail(h,400,'Некоректний товар')
        with connect() as db:
            rows=db.execute('''SELECT substr(c.name,1,30) author,r.id,r.stars,r.body,r.reply,r.created_at
             FROM product_reviews r JOIN customers c ON r.customer_id=c.id
             WHERE r.product_id=? ORDER BY r.created_at DESC LIMIT 100''',(pid,)).fetchall()
        return h.json_response(200,{'reviews':[dict(x) for x in rows]})
    if path.startswith('/api/questions/'):
        try:pid=int(path.rsplit('/',1)[-1]);assert pid>0
        except (ValueError,AssertionError):return fail(h,400,'Некоректний товар')
        with connect() as db:
            rows=db.execute('''SELECT substr(c.name,1,30) author,q.id,q.body,q.reply,q.created_at
             FROM product_questions q JOIN customers c ON q.customer_id=c.id
             WHERE q.product_id=? ORDER BY q.created_at DESC LIMIT 100''',(pid,)).fetchall()
        return h.json_response(200,{'questions':[dict(x) for x in rows]})
    if path=='/api/admin/feedback':
        if not h.session():return fail(h,401,'Потрібен вхід адміністратора')
        with connect() as db:
            rev=db.execute('''SELECT r.id,r.product_id,c.name author,r.stars,r.body,r.reply,r.created_at
                FROM product_reviews r JOIN customers c ON c.id=r.customer_id ORDER BY r.created_at DESC LIMIT 250''').fetchall()
            qs=db.execute('''SELECT q.id,q.product_id,c.name author,q.body,q.reply,q.created_at
                FROM product_questions q JOIN customers c ON c.id=q.customer_id ORDER BY q.created_at DESC LIMIT 250''').fetchall()
        return h.json_response(200,{'reviews':[dict(x) for x in rev],'questions':[dict(x) for x in qs]})
    if path.startswith('/api/admin/confirmation/'):
        if not h.session():return fail(h,401,'Потрібно увійти адміністратору')
        oid=path[len('/api/admin/confirmation/'):]
        if not re.fullmatch(r'FH-\d{8}-[0-9A-F]{8}',oid):return fail(h,400,'Некоректний номер замовлення')
        with connect() as db:
            if not db.execute('SELECT 1 FROM orders WHERE id=?',(oid,)).fetchone():return fail(h,404,'Замовлення не знайдено')
            page=mailer.confirmation_markup(db,oid,printable=True)
        content=page.encode('utf-8')
        h.send_response(200)
        h.send_header('Content-Type','text/html; charset=utf-8')
        h.send_header('Content-Length',str(len(content)))
        h.end_headers()
        h.wfile.write(content)
        return
    if path.startswith('/api/customer/confirmation/'):
        user=require_user(h)
        if not user:return
        oid=path[len('/api/customer/confirmation/'):]
        if not re.fullmatch(r'FH-\d{8}-[0-9A-F]{8}',oid):return fail(h,400,'Неправильний номер замовлення')
        with connect() as db:
            order=db.execute('SELECT 1 FROM orders WHERE id=? AND customer_id=?',(oid,user['id'])).fetchone()
            if order is None:return fail(h,404,'Замовлення не знайдено')
            page=mailer.confirmation_markup(db,oid,printable=True)
        content=page.encode('utf-8')
        h.send_response(200)
        h.send_header('Content-Type','text/html; charset=utf-8')
        h.send_header('Content-Length',str(len(content)))
        h.send_header('Content-Disposition','inline; filename="forma-order.html"')
        h.end_headers()
        h.wfile.write(content)
        return
    if path.startswith('/api/customer/bank-details/'):
        user=require_user(h)
        if not user:return
        oid=path[len('/api/customer/bank-details/'):]
        if not re.fullmatch(r'FH-\d{8}-[0-9A-F]{8}',oid):return fail(h,400,'Некоректний номер')
        with connect() as db:
            row=db.execute('SELECT payment,payment_status,total,status FROM orders WHERE id=? AND customer_id=?',(oid,user['id'])).fetchone()
        if row is None or row['payment']!='invoice':return fail(h,404,'Реквізити не знайдено')
        return h.json_response(200,{'bankPayment':bank_transfer.details(oid,row['total']),
            'paymentStatus':row['payment_status'],'status':row['status']})
    if path=='/api/customer/me':
        s=user_session(h)
        if not s:return h.json_response(200,{'authenticated':False,'onlinePaymentsEnabled':False})
        with connect() as db:
            orders=db.execute('SELECT id,created_at,status,payment,payment_status,delivery,assembly,subtotal,bonus_used,total,bonus_earned,delivery_date,delivery_window,manager_note FROM orders WHERE customer_id=? ORDER BY created_at DESC',(s['id'],)).fetchall()
            safe_orders=[]
            for o in orders:
                item=dict(o)
                item['items']=[dict(r) for r in db.execute('SELECT product_id,title,quantity,unit_price,color_choice FROM order_items WHERE order_id=? ORDER BY rowid',(o['id'],)).fetchall()]
                safe_orders.append(item)
        completed_count=sum(1 for o in safe_orders if o['status']=='fulfilled')
        return h.json_response(200,{'authenticated':True,'user':{'id':s['id'],'name':s['name'],'email':s['email'],'bonus':s['bonus'],'completedOrders':completed_count},'csrf':s['csrf'],'orders':safe_orders,'onlinePaymentsEnabled':False})
    if path=='/api/customer/requests':
        s=require_user(h)
        if not s:return
        with connect() as db:
            rows=db.execute('SELECT * FROM custom_requests WHERE customer_id=? ORDER BY created_at DESC LIMIT 100',(s['id'],)).fetchall()
            requests_out=[]
            for row in rows:
                x=dict(row);x['messages']=[dict(m) for m in db.execute('SELECT sender,body,created_at FROM custom_messages WHERE request_id=? ORDER BY id DESC LIMIT 50',(x['id'],)).fetchall()][::-1]
                requests_out.append(x)
        return h.json_response(200,{'requests':requests_out})
    if path=='/api/admin/orders':
        if not h.session():return fail(h,401,'Потрібен вхід адміністратора')
        with connect() as db:
            data=db.execute('''SELECT o.*,c.name customer_name,c.email customer_email FROM orders o
                JOIN customers c ON c.id=o.customer_id ORDER BY o.created_at DESC LIMIT 500''').fetchall()
            orders=[]
            for o in data:
                item=dict(o)
                item['items']=[dict(r) for r in db.execute('SELECT title,quantity,unit_price,color_choice FROM order_items WHERE order_id=? ORDER BY rowid',(o['id'],)).fetchall()]
                orders.append(item)
        return h.json_response(200,{'orders':orders})
    if path=='/api/admin/contacts':
        if not h.session():return fail(h,401,'Потрібен вхід адміністратора')
        with connect() as db:
            rows=db.execute('SELECT id,name,email,topic,body,status,created_at FROM contact_requests ORDER BY id DESC LIMIT 250').fetchall()
        return h.json_response(200,{'contacts':[dict(r) for r in rows]})
    if path=='/api/admin/customers':
        if not h.session():return fail(h,401,'Потрібен вхід адміністратора')
        with connect() as db:
            data=db.execute('''SELECT c.id,c.name,c.email,c.bonus,c.created_at,COUNT(o.id) orders_count,SUM(CASE WHEN o.status='fulfilled' THEN 1 ELSE 0 END) fulfilled_orders
              FROM customers c LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id ORDER BY c.created_at DESC LIMIT 500''').fetchall()
        return h.json_response(200,{'customers':[dict(r) for r in data]})
    if path=='/api/admin/custom-requests':
        if not h.session():return fail(h,401,'Потрібен вхід адміністратора')
        with connect() as db:
            rows=db.execute('''SELECT x.*,c.name customer_name,c.email customer_email
             FROM custom_requests x JOIN customers c ON x.customer_id=c.id
             ORDER BY x.created_at DESC LIMIT 200''').fetchall()
        result=[]
        with connect() as db:
            for x in rows:
                item=dict(x)
                item['messages']=[dict(m) for m in db.execute('SELECT sender,body,created_at FROM custom_messages WHERE request_id=? ORDER BY id DESC LIMIT 50',(item['id'],)).fetchall()][::-1]
                result.append(item)
        return h.json_response(200,{'requests':result})
    return fail(h,404,'Невідомий API')

# User-submitted data must be validated on the server as well as in the browser.
def valid_customer_name(value):
    if not isinstance(value,str):return False
    value=' '.join(value.split())
    chunks=value.split(' ')
    if not (5<=len(value)<=80 and 2<=len(chunks)<=5):return False
    if any(not re.fullmatch(r"[^\W\d_]+(?:[-'’ʼ][^\W\d_]+)*",chunk,re.UNICODE) for chunk in chunks):return False
    if any(len(re.sub(r"[-'’ʼ]",'',chunk))<2 for chunk in chunks):return False
    if re.search(r'(.)\1{4,}',value,re.IGNORECASE):return False
    if value.casefold() in {'test test','test user','asdf asdf','ім’я прізвище','імя прізвище','name surname'}:return False
    return True

def valid_customer_phone(value):
    if not isinstance(value,str) or not re.fullmatch(r'[+0-9()\s-]{10,24}',value):return False
    digits=re.sub(r'\D','',value)
    if not re.fullmatch(r'(?:380\d{9}|0\d{9})',digits):return False
    return len(set(digits[-9:]))>2 and digits[-9:] not in {'123456789','987654321'}

def valid_customer_city(value):
    if not isinstance(value,str) or not 2<=len(value.strip())<=80:return False
    if not re.fullmatch(r"[^\W\d_]+(?:[ .’'ʼ-][^\W\d_]+|\.)*",value.strip(),re.UNICODE):return False
    return len({c.lower() for c in value if c.isalpha()})>=2 and value.strip().casefold() not in {'тест','test','asdf'}

def valid_customer_address(value):
    if not isinstance(value,str) or not 8<=len(value.strip())<=180:return False
    if any(ord(ch)<32 for ch in value):return False
    return sum(ch.isalpha() for ch in value)>=5 and bool(re.search(r'\d',value)) and len(set(ch.lower() for ch in value if ch.isalpha()))>=3

def valid_new_password(value):
    return (isinstance(value,str) and 10<=len(value)<=256 and not any(c.isspace() for c in value)
            and any(c.isalpha() for c in value) and any(c.isdigit() for c in value)
            and value.lower() not in {'password123','1234567890a','qwerty12345','password1234'})

def signin(h,body,register=False):
    email=str(body.get('email','')).strip().lower()
    password=body.get('password')
    name=str(body.get('name','')).strip()
    if not re.fullmatch(r'[^\s@]{1,90}@[^\s@]{1,90}\.[^\s@]{2,40}',email) or not isinstance(password,str):
        return fail(h,400,'Перевірте email та пароль')
    if register and not valid_customer_name(name):
        return fail(h,400,'Укажіть справжні ім’я та прізвище (тільки літери, два слова)')
    if register and not valid_new_password(password):
        return fail(h,400,'Пароль: від 10 символів, щонайменше одна літера та одна цифра, без пробілів')
    now=int(time.time());key=h.client_address[0]
    with _guard:
        failed=[t for t in _attempts.get(key,[]) if now-t<900];_attempts[key]=failed
        if len(failed)>=8:return fail(h,429,'Забагато спроб входу. Повторіть пізніше.')
    with connect() as db:
        if register:
            salt=secrets.token_hex(20)
            try:
                with db:
                    db.execute('INSERT INTO customers(name,email,password_salt,password_hash,created_at) VALUES (?,?,?,?,?)',(name,email,salt,pwd_hash(password,salt),now))
            except sqlite3.IntegrityError:return fail(h,409,'Цей email вже зареєстрований')
        row=db.execute('SELECT id,name,email,password_hash,password_salt FROM customers WHERE email=?',(email,)).fetchone()
        if row is None or not hmac.compare_digest(row['password_hash'],pwd_hash(password,row['password_salt'])):
            with _guard:_attempts.setdefault(key,[]).append(now)
            return fail(h,401,'Неправильний email або пароль')
        token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32)
        with db:
            db.execute('DELETE FROM customer_sessions WHERE expires<?',(now,))
            db.execute('INSERT INTO customer_sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,now+7*86400))
    with _guard:_attempts[key]=[]
    return h.json_response(200,{'ok':True,'user':{'name':row['name'],'email':row['email']},'csrf':csrf},{'Set-Cookie':cookie_line(token,7*86400)})

def create_order(h,s,body,products):
    items=body.get('items'); delivery=body.get('delivery'); payment=body.get('payment')
    if not isinstance(items,list) or not 1<=len(items)<=40 or delivery not in ('own_standard','own_large','own_priority') or payment not in ('cod','invoice'):
        return fail(h,400,'Перевірте товари, доставку та спосіб оплати')
    receiver=str(body.get('name','')).strip();phone=str(body.get('phone','')).strip()
    city=str(body.get('city','')).strip();address=str(body.get('address','')).strip(); comment=str(body.get('comment','')).strip()
    if not valid_customer_name(receiver):return fail(h,400,'Введіть ім’я та прізвище: два слова без цифр')
    if not valid_customer_phone(phone):return fail(h,400,'Номер телефону має бути українським: +380XXXXXXXXX або 0XXXXXXXXX')
    if not valid_customer_city(city):return fail(h,400,'Вкажіть коректну назву населеного пункту')
    if not valid_customer_address(address):return fail(h,400,'Вкажіть вулицю, номер будинку та за потреби квартиру')
    if len(comment)>500:return fail(h,400,'Коментар завеликий (до 500 символів)')
    assembly=bool(body.get('assembly') is True)
    requested=body.get('useBonus',0)
    if type(requested)!=int or requested<0:return fail(h,400,'Некоректна кількість бонусів')
    found={p['id']:p for p in products}
    clean=[];subtotal=0
    for item in items:
        if not isinstance(item,dict) or type(item.get('id')) is not int or type(item.get('qty')) is not int: return fail(h,400,'Некоректний товар')
        product=found.get(item['id']); qty=item['qty']
        if not product or product.get('active',True) is False or qty<1 or qty>min(int(product.get('stock',0)),99):return fail(h,400,'Недостатньо товару на складі')
        default_color=str(product.get('color') or 'Уточнюється')
        options=product.get('availableColors') or [default_color]
        color=item.get('selectedColor') or default_color
        if not isinstance(color,str):return fail(h,400,'Неправильний колір товару')
        color=color.strip()
        if not 1<=len(color)<=70 or (color not in options and not product.get('allowCustomColor',False)):
            return fail(h,400,'Вибраний колір не доступний для цього товару')
        subtotal+=int(product['price'])*qty
        clean.append((product,qty,color))
    if subtotal<=0 or subtotal>5_000_000:return fail(h,400,'Помилка розрахунку')
    now=int(time.time());oid='FH-'+time.strftime('%Y%m%d')+'-'+secrets.token_hex(4).upper()
    with connect() as db:
        try:
            db.execute('BEGIN IMMEDIATE')
            user=db.execute('SELECT bonus FROM customers WHERE id=?',(s['id'],)).fetchone()
            if user is None: raise ValueError('Користувача не знайдено')
            config=settings(db)
            max_allowed=min(user['bonus'],subtotal*config['bonus_limit']//100)
            if requested>max_allowed:raise ValueError(f'Можна використати не більше {max_allowed} бонусів')
            config=settings(db);shipping=freight(subtotal,delivery,config);assembly_cost=config['assembly_cost'] if assembly else 0
            total=subtotal+shipping+assembly_cost-requested
            db.execute('UPDATE customers SET bonus=bonus-? WHERE id=?',(requested,s['id']))
            db.execute('''INSERT INTO orders(id,customer_id,created_at,status,payment,payment_status,delivery,receiver,phone,city,address,assembly,subtotal,delivery_cost,assembly_cost,bonus_used,total,comment)
                       VALUES (?,?,?,'new',?,'unpaid',?,?,?,?,?,?,?,?,?,?,?,?)''',
                (oid,s['id'],now,payment,delivery,receiver,phone,city,address,int(assembly),subtotal,shipping,assembly_cost,requested,total,comment))
            for product,qty,color in clean:
                db.execute('INSERT INTO order_items(order_id,product_id,title,quantity,unit_price,color_choice) VALUES (?,?,?,?,?,?)',
                    (oid,product['id'],product['name'],qty,int(product['price']),color))
            if requested:db.execute('INSERT INTO loyalty_events(customer_id,order_id,delta,reason,created_at) VALUES (?,?,?,?,?)',(s['id'],oid,-requested,'Використання бонусів',now))
            mailer.receipt(db,oid,'created')
            db.commit()
        except ValueError as e:
            db.rollback();return fail(h,400,str(e))
    mailer.launch(connect)
    out={'ok':True,'orderId':oid,'total':total,'subtotal':subtotal,'deliveryCost':shipping,'assemblyCost':assembly_cost,'bonusUsed':requested,'status':'new','paymentStatus':'unpaid','emailQueued':True}
    if payment=='invoice':out['bankPayment']=bank_transfer.details(oid,total)
    return h.json_response(200,out)

ALLOWED_STATES={
 'new':{'accepted','cancelled'},
 'accepted':{'processing','scheduled','fulfilled','cancelled'},
 'processing':{'scheduled','fulfilled','cancelled'},
 'scheduled':{'in_delivery','fulfilled','cancelled','processing'},
 'in_delivery':{'fulfilled','cancelled'},
 'fulfilled':set(), 'cancelled':set()
}


def admin_order_status(h,body):
    oid=body.get('orderId');new=body.get('status')
    date=body.get('deliveryDate','') or ''
    window=body.get('deliveryWindow','') or ''
    note=body.get('managerNote','') or ''
    if not isinstance(oid,str) or new not in ALLOWED_STATES or not isinstance(date,str) or not isinstance(window,str) or not isinstance(note,str):
        return fail(h,400,'Перевірте статус і дані доставки')
    if len(note)>350 or len(window)>50:
        return fail(h,400,'Занадто довгий коментар або інтервал')
    if date:
        try:
            parsed=datetime.date.fromisoformat(date)
            if parsed < datetime.date.today() or parsed>datetime.date.today()+datetime.timedelta(days=366):raise ValueError()
        except ValueError:
            return fail(h,400,'Укажіть коректну дату доставки')
    if window and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d\s*[-–]\s*([01]\d|2[0-3]):[0-5]\d',window):
        return fail(h,400,'Інтервал часу: 10:00–14:00')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        o=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o:return fail(h,404,'Замовлення не знайдено')
        current=o['status']
        # Editable delivery details while status remains unchanged.
        if new!=current and new not in ALLOWED_STATES.get(current,set()):
            return fail(h,409,'Перехід між статусами недоступний')
        final_date=date or o['delivery_date']
        final_window=window or o['delivery_window']
        if new in ('scheduled','in_delivery','fulfilled') and (not final_date or not final_window):
            return fail(h,400,'Перед відправленням вкажіть дату й інтервал доставки')
        if o['payment']=='invoice' and o['payment_status']!='paid' and new in ('scheduled','in_delivery','fulfilled'):
            return fail(h,409,'Спочатку перевірте надходження коштів у банку й підтвердьте оплату')
        if new=='fulfilled' and o['payment']=='online' and o['payment_status']!='paid':
            return fail(h,409,'Потрібне підтвердження платежу провайдером')
        if new=='fulfilled' and o['payment']=='cod' and body.get('cashReceived') is not True:
            return fail(h,400,'Підтвердьте, що оплата при отриманні дійсно отримана від покупця')
        earned=0
        if new=='cancelled' and current!='cancelled':
            refund=o['bonus_used']
            if refund:
                db.execute('UPDATE customers SET bonus=bonus+? WHERE id=?',(refund,o['customer_id']))
                db.execute('INSERT INTO loyalty_events(customer_id,order_id,delta,reason,created_at) VALUES (?,?,?,?,?)',(o['customer_id'],oid,refund,'Повернення при скасуванні',int(time.time())))
        if new=='fulfilled' and current!='fulfilled':
            conf=settings(db)
            previous=db.execute("SELECT COUNT(*) AS n FROM orders WHERE customer_id=? AND status='fulfilled' AND id<>?",(o['customer_id'],oid)).fetchone()['n']
            rate=conf['bonus_rate_loyal'] if previous >= conf['bonus_regular_after'] else conf['bonus_rate']
            earned=max(0,o['subtotal']-o['bonus_used'])*rate//100
            if earned:
                db.execute('UPDATE customers SET bonus=bonus+? WHERE id=?',(earned,o['customer_id']))
                db.execute('INSERT INTO loyalty_events(customer_id,order_id,delta,reason,created_at) VALUES (?,?,?,?,?)',(o['customer_id'],oid,earned,f'Бонуси за виконане замовлення · {rate}%',int(time.time())))
        payment_status='paid' if new=='fulfilled' and o['payment']=='cod' else o['payment_status']
        db.execute('UPDATE orders SET status=?,payment_status=?,delivery_date=?,delivery_window=?,manager_note=?,bonus_earned=bonus_earned+? WHERE id=?',
                  (new,payment_status,final_date,final_window,note or o['manager_note'],earned,oid))
        if new!=current or final_date!=o['delivery_date'] or final_window!=o['delivery_window'] or note and note!=o['manager_note']:
            db.execute('INSERT INTO order_events(order_id,status,delivery_date,delivery_window,comment,created_at) VALUES (?,?,?,?,?,?)',
                       (oid,new,final_date,final_window,note,int(time.time())))
            mailer.receipt(db,oid,'updated')
        db.commit()
    mailer.launch(connect)
    return h.json_response(200,{'ok':True,'status':new,'bonusEarned':earned,'deliveryDate':final_date,'deliveryWindow':final_window,'paymentStatus':payment_status,'emailQueued':True})


def admin_confirm_payment(h,body):
    """Manual proof from the owner: never accept a customer-reported payment as proof."""
    oid=body.get('orderId')
    reference=body.get('bankReference')
    checked=body.get('verified')
    if not isinstance(oid,str) or not re.fullmatch(r'FH-\d{8}-[0-9A-F]{8}',oid):
        return fail(h,400,'Некоректне замовлення')
    if checked is not True or not isinstance(reference,str) or not 4<=len(reference.strip())<=120:
        return fail(h,400,'Перевірте операцію в банку та вкажіть номер або опис транзакції (від 4 символів)')
    now=int(time.time())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        o=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o:return fail(h,404,'Замовлення не знайдено')
        if o['payment']!='invoice':return fail(h,409,'Підтвердження призначене лише для IBAN-переказів')
        if o['status']=='cancelled':return fail(h,409,'Скасоване замовлення не можна позначити оплаченим')
        if o['payment_status']=='paid' or db.execute('SELECT 1 FROM payment_confirmations WHERE order_id=?',(oid,)).fetchone():
            return fail(h,409,'Цей платіж уже підтверджено')
        with db:
            db.execute('INSERT INTO payment_confirmations(order_id,amount,bank_reference,confirmed_at) VALUES (?,?,?,?)',
                       (oid,o['total'],reference.strip(),now))
            newstatus='accepted' if o['status']=='new' else o['status']
            db.execute("UPDATE orders SET payment_status='paid',status=? WHERE id=? AND payment_status='unpaid'",(newstatus,oid))
            db.execute('INSERT INTO order_events(order_id,status,delivery_date,delivery_window,comment,created_at) VALUES (?,?,?,?,?,?)',
                       (oid,newstatus,o['delivery_date'],o['delivery_window'],'Адміністратор підтвердив IBAN-переказ після перевірки виписки банку',now))
            mailer.receipt(db,oid,'payment_confirmed')
    mailer.launch(connect)
    return h.json_response(200,{'ok':True,'orderId':oid,'paymentStatus':'paid','status':newstatus,'emailQueued':True})


def admin_settings(h,body):
    changes=body.get('settings')
    if not isinstance(changes,dict) or not changes or any(k not in DEFAULT_SETTINGS for k in changes):
        return fail(h,400,'Некоректні налаштування тарифів')
    bounds={'own_standard':(0,50000),'own_large':(0,50000),'own_priority':(0,50000),'assembly_cost':(0,100000),'free_standard_over':(0,3000000),'bonus_rate':(0,10),'bonus_rate_loyal':(0,10),'bonus_regular_after':(1,20),'bonus_limit':(0,50)}
    for key,value in changes.items():
        lo,hi=bounds[key]
        if type(value) is not int or not lo<=value<=hi:return fail(h,400,'Неприпустиме значення '+key)
    with connect() as db:
        with db:
            for key,value in changes.items():db.execute('UPDATE commerce_settings SET value=? WHERE name=?',(value,key))
    return h.json_response(200,{'ok':True,'settings':settings()})


def admin_mail_action(h,body):
    action=body.get('action')
    if action=='send_pending':
        with connect() as db:
            with db: db.execute("UPDATE email_queue SET status='pending' WHERE status='failed'")
        return h.json_response(200,{'ok':True,**mailer.process_pending(connect,limit=25)})
    if action=='test':
        if not mailer.configured():return fail(h,409,'Спершу налаштуйте Gmail через configure_mail.py')
        with connect() as db:
            with db: mailer.queue(db,mailer.FROM_ADDRESS,'Forma Home · Перевірка пошти',
                'Тестове повідомлення Forma Home. Надсилання листів налаштовано.','',kind='test')
        return h.json_response(200,{'ok':True,**mailer.process_pending(connect,limit=25)})
    return fail(h,400,'Невідома поштова дія')

def callback(h):
    return fail(h,410,'Оплата карткою вимкнена')

def post(h,path):
    if path=='/api/contact':
        body=h.valid_post(4500)
        if body is None:return
        name=str(body.get('name','')).strip()
        email=str(body.get('email','')).strip().lower()
        topic=str(body.get('topic','')).strip()
        message=str(body.get('message','')).strip()
        if not 2<=len(name)<=80 or not re.fullmatch(r'[^\s@]{1,90}@[^\s@]{1,90}\.[^\s@]{2,30}',email) or topic not in ('Питання про товар','Доставка та оплата','Повернення','Інше питання') or not 10<=len(message)<=2000:
            return fail(h,400,'Перевірте поля звернення')
        now=int(time.time()); ip=h.client_address[0]; key='contact:'+ip
        with _guard:
            recent=[t for t in _attempts.get(key,[]) if now-t<1800]
            if len(recent)>=6:return fail(h,429,'Забагато звернень. Спробуйте пізніше.')
            recent.append(now);_attempts[key]=recent
        with connect() as db:
            with db: cur=db.execute('INSERT INTO contact_requests(name,email,topic,body,created_at) VALUES (?,?,?,?,?)',(name,email,topic,message,now))
        return h.json_response(200,{'ok':True,'id':cur.lastrowid})
    if path=='/api/admin/contact/status':
        if not h.require_admin():return
        body=h.valid_post(1500)
        if body is None:return
        rid=body.get('id'); status=body.get('status')
        if type(rid) is not int or status not in ('new','processed'):return fail(h,400,'Некоректний статус')
        with connect() as db:
            with db:cur=db.execute('UPDATE contact_requests SET status=? WHERE id=?',(status,rid))
        return h.json_response(200,{'ok':True}) if cur.rowcount else fail(h,404,'Звернення не знайдено')
    if path=='/api/customer/question':
        s=require_user(h,True)
        if not s:return
        body=h.valid_post(3000)
        if body is None:return
        pid=body.get('productId');txt=str(body.get('body','')).strip()
        if type(pid) is not int or not (8<=len(txt)<=1200):return fail(h,400,'Запитання від 8 до 1200 символів')
        products=json.loads((ROOT/'products.json').read_text(encoding='utf8'))
        if not any(p['id']==pid and p.get('active',True) for p in products):return fail(h,404,'Товар не знайдено')
        with connect() as db:
            with db:db.execute('INSERT INTO product_questions(customer_id,product_id,body,created_at) VALUES (?,?,?,?)',(s['id'],pid,txt,int(time.time())))
        return h.json_response(200,{'ok':True})
    if path=='/api/admin/feedback/reply':
        if not h.require_admin():return
        body=h.valid_post(4000)
        if body is None:return
        kind=body.get('kind');rid=body.get('id');reply=str(body.get('reply','')).strip()
        if kind not in ('question','review') or type(rid) is not int or not (1<=len(reply)<=2000):return fail(h,400,'Неправильна відповідь')
        table='product_questions' if kind=='question' else 'product_reviews'
        with connect() as db:
            with db:cursor=db.execute(f'UPDATE {table} SET reply=? WHERE id=?',(reply,rid))
        if not cursor.rowcount:return fail(h,404,'Запис не знайдено')
        return h.json_response(200,{'ok':True})
    if path=='/api/customer/review':
        s=require_user(h,True)
        if not s:return
        body=h.valid_post(3000)
        if body is None:return
        pid=body.get('productId');stars=body.get('stars');content=str(body.get('body','')).strip()
        if type(pid) is not int or type(stars) is not int or not 1<=stars<=5 or not 10<=len(content)<=1200:
            return fail(h,400,'Перевірте оцінку та відгук (10–1200 символів)')
        products=json.loads((ROOT/'products.json').read_text(encoding='utf-8'))
        if not any(p['id']==pid for p in products):return fail(h,404,'Товар не знайдено')
        with connect() as db:
            try:
                with db:db.execute('INSERT INTO product_reviews(customer_id,product_id,stars,body,created_at) VALUES (?,?,?,?,?)',(s['id'],pid,stars,content,int(time.time())))
            except sqlite3.IntegrityError:return fail(h,409,'Ви вже залишили відгук для цього товару')
        return h.json_response(200,{'ok':True})
    if path=='/api/customer/signup':
        body=h.valid_post(5000);return signin(h,body,True) if body is not None else None
    if path=='/api/customer/login':
        body=h.valid_post(5000);return signin(h,body) if body is not None else None
    if path=='/api/customer/logout':
        s=require_user(h,True)
        if not s:return
        tok=''
        for x in h.headers.get('Cookie','').split(';'):
            k,sep,v=x.strip().partition('=')
            if sep and k=='FHCUST':tok=v
        with connect() as db:db.execute('DELETE FROM customer_sessions WHERE token_hash=?',(hashlib.sha256(tok.encode()).hexdigest(),))
        return h.json_response(200,{'ok':True},{'Set-Cookie':cookie_line('',0)})
    if path=='/api/customer/order':
        s=require_user(h,True)
        if not s:return
        body=h.valid_post(20000)
        if body is None:return
        return create_order(h,s,body,json.loads((ROOT/'products.json').read_text(encoding='utf-8')))
    if path in ('/api/customer/custom-message','/api/admin/custom-message'):
        admin=path.startswith('/api/admin/')
        if admin:
            if not h.require_admin():return
        else:
            user=require_user(h,True)
            if not user:return
        body=h.valid_post(3500)
        if body is None:return
        reqid=body.get('requestId');msg=str(body.get('body','')).strip()
        if not isinstance(reqid,str) or len(reqid)>60 or not 2<=len(msg)<=1000:return fail(h,400,'Повідомлення: від 2 до 1000 символів')
        with connect() as db:
            row=db.execute('SELECT customer_id FROM custom_requests WHERE id=?',(reqid,)).fetchone()
            if not row or (not admin and row['customer_id']!=user['id']):return fail(h,404,'Заявку не знайдено')
            with db:db.execute('INSERT INTO custom_messages(request_id,sender,body,created_at) VALUES (?,?,?,?)',(reqid,'manager' if admin else 'customer',msg,int(time.time())))
        return h.json_response(200,{'ok':True})
    if path=='/api/customer/custom-request':
        s=require_user(h,True)
        if not s:return
        body=h.valid_post(5000)
        if body is None:return
        cat=str(body.get('category','')).strip()
        phone=str(body.get('phone','')).strip()
        dims=str(body.get('dimensions','')).strip()
        details=str(body.get('details','')).strip()
        budget=body.get('budget')
        choices={'Кухня під замовлення','Обідній стіл під замовлення','Стільці під замовлення','Шафа / вбудовані меблі','Килим нестандартного розміру','Інше'}
        if cat not in choices or not valid_customer_phone(phone) or len(dims)>160 or not 15<=len(details)<=1800 or budget is not None and (type(budget) is not int or not 0<=budget<=1000000):
            return fail(h,400,'Перевірте контактні дані та опис запиту')
        oid='REQ-'+time.strftime('%Y%m%d')+'-'+secrets.token_hex(4).upper()
        with connect() as db:
            with db:
                db.execute('''INSERT INTO custom_requests(id,customer_id,created_at,category,phone,dimensions,budget,details)
                    VALUES (?,?,?,?,?,?,?,?)''',(oid,s['id'],int(time.time()),cat,phone,dims,budget,details))
        with connect() as db:
            with db:db.execute('INSERT INTO custom_messages(request_id,sender,body,created_at) VALUES (?,?,?,?)',(oid,'customer',details,int(time.time())))
        return h.json_response(200,{'ok':True,'requestId':oid})
    if path=='/api/admin/custom-request/status':
        if not h.require_admin():return
        body=h.valid_post(3000)
        if body is None:return
        rid=body.get('requestId');status=body.get('status')
        if not isinstance(rid,str) or status not in ('new','in_progress','answered','cancelled'):
            return fail(h,400,'Некоректний статус')
        with connect() as db:
            with db:
                result=db.execute('UPDATE custom_requests SET status=? WHERE id=?',(status,rid))
        return h.json_response(200,{'ok':True}) if result.rowcount else fail(h,404,'Заявку не знайдено')
    if path=='/api/admin/commerce/settings':
        if not h.require_admin():return
        body=h.valid_post(3000)
        return admin_settings(h,body) if body is not None else None
    if path=='/api/admin/mail/action':
        if not h.require_admin():return
        body=h.valid_post(3000)
        return admin_mail_action(h,body) if body is not None else None
    if path=='/api/admin/order/payment-confirm':
        if not h.require_admin():return
        body=h.valid_post(4000)
        return admin_confirm_payment(h,body) if body is not None else None
    if path=='/api/admin/order/status':
        if not h.require_admin():return
        body=h.valid_post(3000)
        return admin_order_status(h,body) if body is not None else None
    if path=='/api/payments/liqpay/callback':return callback(h)
    return fail(h,404,'Невідомий API')
