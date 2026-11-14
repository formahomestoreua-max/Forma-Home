#!/usr/bin/env python3
"""Forma Home local catalog server, no third-party Python dependencies.
Binds to loopback only. Not an internet-facing production service.
"""
from __future__ import annotations
import base64, hashlib, hmac, json, mimetypes, os, re, secrets, socket, sys, time, webbrowser, shutil, traceback
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, unquote
from getpass import getpass
from threading import Lock
import loyalty, sitecms

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / '.forma_admin_config.json'
CATALOG = ROOT / 'products.json'
PRODUCT_SEQUENCE = ROOT / '.forma_product_sequence.json'
DELETE_LOG = ROOT / '.forma_deleted_products.json'
BACKUP_DIR = ROOT / '.forma_backups'
IMAGE_DIR = ROOT / 'assets' / 'photos'
SESSIONS = {}
ATTEMPTS = {}
LOCK = Lock()
MAX_JSON = 10 * 1024 * 1024
ADMIN_FIELDS = {'name','category','price','oldPrice','stock','color','material','dimensions','width','depth','height','weight','series','warranty','assembly','description','manual','featured','new','active','availableColors','allowCustomColor'}


def read_json(p, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


class CatalogDataError(Exception):
    """Protect an existing catalog from being silently replaced with []."""


def read_catalog():
    if not CATALOG.is_file():
        raise CatalogDataError('Не знайдено products.json. Перевірте папку сайту та резервну копію.')
    try:
        data=json.loads(CATALOG.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CatalogDataError('Файл products.json пошкоджений або недоступний. Дані не перезаписані.') from exc
    if not isinstance(data,list) or any(not isinstance(x,dict) or type(x.get('id')) is not int or not isinstance(x.get('name'),str) or not isinstance(x.get('sku'),str) or not isinstance(x.get('images'),list) for x in data):
        raise CatalogDataError('Неправильна структура products.json. Зміни заблоковано, щоб зберегти дані.')
    if len({p['id'] for p in data})!=len(data):
        raise CatalogDataError('У products.json повторюються артикули товарів. Перевірте резервну копію.')
    return data


def backup_catalog():
    """Keep a bounded set of automatic pre-change snapshots (no customer data)."""
    if not CATALOG.is_file(): return
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    name=f'catalog-{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}.json'
    shutil.copy2(CATALOG,BACKUP_DIR/name)
    for old in sorted(BACKUP_DIR.glob('catalog-*.json'),key=lambda f:f.stat().st_mtime,reverse=True)[12:]:
        try:old.unlink()
        except OSError:pass


def write_atomic(path, text):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)



def next_product_id(catalog):
    # A deleted SKU must never be reused: order history refers to product IDs.
    previous = read_json(PRODUCT_SEQUENCE, {})
    previous_id = previous.get('lastId', 0) if isinstance(previous, dict) else 0
    previous_id = previous_id if type(previous_id) is int and previous_id >= 0 else 0
    deleted=read_json(DELETE_LOG,[])
    ids=[p.get('id',0) for p in catalog]
    if isinstance(deleted,list):
        ids.extend(x.get('id',0) for x in deleted if isinstance(x,dict))
    return max([previous_id]+[x for x in ids if type(x) is int and x>=0])+1


def record_product_id(pid):
    previous = read_json(PRODUCT_SEQUENCE, {})
    last = previous.get('lastId', 0) if isinstance(previous, dict) else 0
    write_atomic(PRODUCT_SEQUENCE, json.dumps({'lastId': max(pid, last if type(last) is int else 0)}))


def setup_password():
    if CONFIG.exists():
        return
    print('\nПЕРШИЙ ЗАПУСК — НАЛАШТУВАННЯ АДМІНПАНЕЛІ')
    print('Пароль задається лише на вашому комп’ютері, у ZIP стандартного пароля немає.')
    while True:
        a = getpass('Створіть пароль адміністратора (мінімум 12 символів): ')
        b = getpass('Повторіть пароль: ')
        if len(a) < 12:
            print('Пароль має бути довжиною від 12 символів.')
        elif a != b:
            print('Паролі не збігаються.')
        else:
            break
    salt = secrets.token_bytes(24)
    dk = hashlib.pbkdf2_hmac('sha256', a.encode('utf-8'), salt, 600_000)
    write_atomic(CONFIG, json.dumps({'salt': salt.hex(), 'hash': dk.hex(), 'iterations':600000}))
    try: os.chmod(CONFIG,0o600)
    except OSError: pass
    print('✓ Пароль налаштовано. Доступ до /admin захищений.\n')


def check_password(password):
    data = read_json(CONFIG, {})
    try:
        salt = bytes.fromhex(data['salt'])
        check = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, int(data['iterations']))
        return hmac.compare_digest(check, bytes.fromhex(data['hash']))
    except (KeyError, TypeError, ValueError):
        return False


def cookie_get(header, name):
    for part in (header or '').split(';'):
        k, sep, v = part.strip().partition('=')
        if sep and k == name: return v
    return ''


class Handler(SimpleHTTPRequestHandler):
    server_version = 'FormaHomeLocal/1.0'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Cache-Control','no-store')
        super().end_headers()

    def log_message(self,fmt,*args):
        print('[%s] %s' % (self.log_date_time_string(),fmt % args))

    def json_response(self, status, data, extra_headers=None):
        raw=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        for k,v in (extra_headers or {}).items(): self.send_header(k,v)
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, status, path):
        raw=path.read_bytes()
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self,url):
        self.send_response(303)
        self.send_header('Location',url)
        self.send_header('Content-Length','0')
        self.end_headers()

    def session(self):
        token=cookie_get(self.headers.get('Cookie'),'FHSESS')
        with LOCK:
            s=SESSIONS.get(token)
            if not s or s['expires'] < time.time():
                SESSIONS.pop(token,None)
                return None
            return s

    def path_part(self):
        return urlsplit(self.path).path

    def do_GET(self):
        try: return self.route_GET()
        except CatalogDataError as exc:
            return self.json_response(503,{'error':str(exc)})
        except (BrokenPipeError, ConnectionResetError): return
        except Exception:
            traceback.print_exc()
            try:return self.json_response(500,{'error':'Помилка сервера. Подивіться повідомлення у вікні CMD.'})
            except (BrokenPipeError, ConnectionResetError): return

    def route_GET(self):
        path=unquote(self.path_part())
        if path=='/admin' or path=='/admin/':
            if self.session(): return self.send_html(200,ROOT/'admin.html')
            return self.redirect('/admin/login')
        if path=='/admin/login':
            if self.session(): return self.redirect('/admin')
            return self.send_html(200,ROOT/'login.html')
        if path=='/api/admin/session':
            s=self.session()
            return self.json_response(200,{'ok':True,'csrf':s['csrf']}) if s else self.json_response(401,{'error':'Потрібно увійти'})
        if path=='/api/admin/content':
            if not self.session(): return self.json_response(401,{'error':'Потрібно увійти'})
            return self.json_response(200,sitecms.read())
        if path=='/api/admin/catalog':
            if not self.session(): return self.json_response(401,{'error':'Потрібно увійти'})
            data=read_catalog()
            return self.json_response(200,{'products':data,'nextSku':f'FH-{next_product_id(data):04d}'})
        if path.startswith('/api/'):
            return loyalty.get(self,path)
        if path in ('/admin.html','/login.html'):
            return self.redirect('/admin')
        if self.blocked(path):
            return self.send_error(404,'Not Found')
        if path=='/': self.path='/index.html'
        return super().do_GET()

    @staticmethod
    def blocked(path):
        if '\\' in path or '\x00' in path: return True
        parts=Path(path).parts
        if any(x.startswith('.') for x in parts if x not in ('/','')): return True
        base=Path(path).name.lower()
        return base.endswith(('.py','.bat','.sh','.tmp','.bak','.zip','.sqlite3','.sqlite3-wal','.sqlite3-shm','.db','.env','.example')) or base in ('admin.html','login.html','readme.md')

    def valid_post(self, size_limit=MAX_JSON):
        # Cross-site write protection: same-origin + secret CSRF on admin writes.
        origin=self.headers.get('Origin')
        host=self.headers.get('Host','')
        if origin and urlsplit(origin).netloc!=host:
            self.json_response(403,{'error':'Недозволене джерело запиту'})
            return None
        try: length=int(self.headers.get('Content-Length','-1'))
        except ValueError: length=-1
        if not 0 < length <= size_limit:
            self.json_response(413,{'error':'Неприпустимий розмір запиту'})
            return None
        try:
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict): raise ValueError()
            return payload
        except (json.JSONDecodeError,ValueError,UnicodeDecodeError):
            self.json_response(400,{'error':'Некоректний JSON'})
            return None

    def require_admin(self):
        s=self.session()
        if s is None:
            self.json_response(401,{'error':'Потрібно увійти'})
            return None
        if not hmac.compare_digest(s['csrf'],self.headers.get('X-FH-CSRF','')):
            self.json_response(403,{'error':'Не пройдена CSRF-перевірка'})
            return None
        return s

    def do_POST(self):
        try: return self.route_POST()
        except CatalogDataError as exc:
            return self.json_response(503,{'error':str(exc)})
        except (BrokenPipeError, ConnectionResetError): return
        except Exception:
            traceback.print_exc()
            try:return self.json_response(500,{'error':'Помилка збереження на сервері. Дані не видаляйте; дивіться CMD.'})
            except (BrokenPipeError, ConnectionResetError): return

    def route_POST(self):
        path=self.path_part()
        if path=='/api/admin/mail/action' or path=='/api/admin/commerce/settings' or path=='/api/contact' or path=='/api/admin/contact/status' or path.startswith('/api/customer/') or path=='/api/admin/feedback/reply' or path.startswith('/api/admin/order/') or path.startswith('/api/admin/custom-request/') or path=='/api/admin/custom-message' or path=='/api/payments/liqpay/callback':
            return loyalty.post(self,path)
        if path=='/api/login':
            body=self.valid_post(4096)
            if body is None: return
            now=time.time()
            ip=self.client_address[0]
            with LOCK:
                failures=[t for t in ATTEMPTS.get(ip,[]) if now-t < 900]
                ATTEMPTS[ip]=failures
                too_many=len(failures)>=5
            if too_many: return self.json_response(429,{'error':'Забагато спроб. Спробуйте пізніше.'})
            if not isinstance(body.get('password'),str) or not check_password(body['password']):
                with LOCK: ATTEMPTS.setdefault(ip,[]).append(now)
                return self.json_response(401,{'error':'Неправильний пароль'})
            token=secrets.token_urlsafe(40)
            with LOCK:
                ATTEMPTS[ip]=[]
                SESSIONS[token]={'csrf':secrets.token_urlsafe(32),'expires':now+8*3600}
            return self.json_response(200,{'ok':True},{'Set-Cookie':f'FHSESS={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800'})
        if path=='/api/admin/logout':
            token=cookie_get(self.headers.get('Cookie'),'FHSESS')
            with LOCK: SESSIONS.pop(token,None)
            return self.json_response(200,{'ok':True},{'Set-Cookie':'FHSESS=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path not in ('/api/admin/product','/api/admin/photo','/api/admin/photo/remove','/api/admin/product/create','/api/admin/product/collections','/api/admin/category','/api/admin/product/duplicate','/api/admin/product/delete','/api/admin/site','/api/admin/banner-photo'):
            return self.json_response(404,{'error':'Невідомий маршрут'})
        if not self.require_admin(): return
        body=self.valid_post()
        if body is None: return
        if path in ('/api/admin/site','/api/admin/banner-photo'): return sitecms.post(self,path,body)
        if path=='/api/admin/product': return self.save_product(body)
        if path=='/api/admin/product/create': return self.create_product(body)
        if path=='/api/admin/product/collections': return self.product_collections(body)
        if path=='/api/admin/category': return self.manage_category(body)
        if path=='/api/admin/product/duplicate': return self.duplicate_product(body)
        if path=='/api/admin/product/delete': return self.delete_product(body)
        if path=='/api/admin/photo/remove': return self.remove_photo(body)
        return self.save_photo(body)

    def create_product(self,body):
        category=str(body.get('category','')).strip()
        name=str(body.get('name','')).strip()
        if len(name)<3 or len(name)>150 or len(category)<3 or len(category)>80:
            return self.json_response(400,{'error':'Вкажіть назву та категорію'})
        collection_ids=body.get('collectionIds',[])
        if not isinstance(collection_ids,list) or len(collection_ids)>40 or any(not isinstance(x,str) for x in collection_ids):
            return self.json_response(400,{'error':'Некоректний вибір колекцій'})
        with LOCK:
            site_content=sitecms.read()
            category_rows=site_content.setdefault('categories',[])
            new_category=not any(c.get('title','').casefold()==category.casefold() for c in category_rows)
            if new_category:
                category_rows.append({'title':category,'group':'Інше','image':'','active':True})
            available={x['id'] for x in site_content.get('collections',[])}
            if not set(collection_ids).issubset(available):
                return self.json_response(400,{'error':'Колекцію не знайдено. Оновіть редактор.'})
            data=read_catalog()
            pid=next_product_id(data)
            # The last product may have been deleted. A new one must still be creatable.
            source=data[0] if data else {
                'kind':'other','weight':'Уточнюється','width':0,'depth':0,'height':0,
                'rating':0,'reviews':0,'images':[], 'description':'',
            }
            new=dict(source)
            price=body.get('price',1000);old=body.get('oldPrice');stock=body.get('stock',0)
            if type(price) is not int or not 1<=price<=10000000 or type(stock) is not int or not 0<=stock<=100000 or old is not None and (type(old) is not int or old<=price or old>10000000):
                return self.json_response(400,{'error':'Неправильні ціна, стара ціна або залишок'})
            flags={key:body.get(key,key!='featured') for key in ('featured','new','active')}
            if any(type(v) is not bool for v in flags.values()):return self.json_response(400,{'error':'Некоректні прапорці'})
            new.update(id=pid,sku=f'FH-{pid:04d}',slug=f'new-model-{pid}',name=name,category=category,price=price,oldPrice=old,
                images=['assets/photos/new-product-placeholder.svg'],featured=flags['featured'],new=flags['new'],active=flags['active'],stock=stock,reviews=0,
                rating=0,description='Опис нового товару — відредагуйте в адмінпанелі.',color='Уточнюється',material='Уточнюється',
                dimensions='Уточнюється',warranty='Уточнюється',assembly='Уточнюється',manual='',width=0,depth=0,height=0,weight='Уточнюється',availableColors=[],allowCustomColor=False)
            colors=body.get('availableColors',[]); allow=body.get('allowCustomColor',False)
            if not self.valid_colors(colors) or type(allow) is not bool:
                return self.json_response(400,{'error':'Перевірте доступні кольори'})
            new['availableColors']=colors;new['allowCustomColor']=allow
            new['color']=colors[0] if colors else 'За погодженням' if allow else 'Уточнюється'
            data.append(new)
            record_product_id(pid)
            self.save_catalog_files(data)
            for collection in site_content.get('collections',[]):
                if collection['id'] in collection_ids:
                    collection.setdefault('productIds',[]).append(pid)
            if collection_ids or new_category:sitecms.save(site_content)
        return self.json_response(200,{'ok':True,'product':new,'collectionIds':collection_ids})

    def product_collections(self,body):
        """Assign an existing product to 0..N curated collections (authenticated + CSRF)."""
        pid=body.get('productId'); ids=body.get('collectionIds')
        if type(pid) is not int or not isinstance(ids,list) or len(ids)>40 or any(not isinstance(x,str) for x in ids):
            return self.json_response(400,{'error':'Некоректний список колекцій'})
        with LOCK:
            catalog=read_catalog()
            if not any(p.get('id')==pid for p in catalog):
                return self.json_response(404,{'error':'Товар не знайдено'})
            content=sitecms.read()
            available={c['id'] for c in content.get('collections',[])}
            if not set(ids).issubset(available):
                return self.json_response(400,{'error':'Такої колекції більше немає. Оновіть сторінку.'})
            for collection in content.get('collections',[]):
                product_ids=[x for x in collection.get('productIds',[]) if x!=pid]
                if collection['id'] in ids: product_ids.append(pid)
                collection['productIds']=product_ids
            sitecms.save(content)
        return self.json_response(200,{'ok':True,'productId':pid,'collectionIds':ids})

    @staticmethod
    def valid_colors(colors):
        return (isinstance(colors,list) and len(colors)<=16 and all(isinstance(c,str) and 1<=len(c.strip())<=40 for c in colors)
                and len({c.casefold().strip() for c in colors})==len(colors))

    def manage_category(self,body):
        action=body.get('action'); title=body.get('title',''); old=body.get('oldTitle','')
        if action not in ('save','delete') or not isinstance(title,str) or not 2<=len(title.strip())<=70 or (old and not isinstance(old,str)):
            return self.json_response(400,{'error':'Перевірте назву категорії'})
        title=title.strip(); group=body.get('group','Інше');image=body.get('image','')
        groups=('Меблі','Кухня','Килими й освітлення','Декор і свято','Текстиль та декор','Свято','Інше')
        if group not in groups or not isinstance(image,str) or (image and (not image.startswith(('assets/categories/','assets/campaigns/')) or '..' in image)):
            return self.json_response(400,{'error':'Некоректна група або фотографія'})
        with LOCK:
            catalog=read_catalog(); data=sitecms.read(); rows=data.setdefault('categories',[])
            previous=next((r for r in rows if r['title'].casefold()==(old or title).casefold()),None)
            in_use=any(p.get('category')==(old or title) for p in catalog)
            if action=='delete':
                if in_use:return self.json_response(409,{'error':'Категорія містить товари. Перенесіть їх перед видаленням.'})
                data['categories']=[r for r in rows if r['title']!=title]
                sitecms.save(data)
                return self.json_response(200,{'ok':True,'categories':data['categories']})
            if any(r['title'].casefold()==title.casefold() and r is not previous for r in rows) or (old and old!=title and any(p['category'].casefold()==title.casefold() for p in catalog)):
                return self.json_response(409,{'error':'Категорія з такою назвою вже існує'})
            if len(rows)>=100 and not previous:return self.json_response(400,{'error':'Досягнуто ліміт категорій'})
            if previous:previous.update(title=title,group=group,image=image,active=True)
            else:rows.append({'title':title,'group':group,'image':image,'active':True})
            if old and old!=title:
                for p in catalog:
                    if p.get('category')==old:p['category']=title
                self.save_catalog_files(catalog)
            sitecms.save(data)
        return self.json_response(200,{'ok':True,'categories':data['categories']})

    @staticmethod
    def save_catalog_files(data):
        # Preserve an on-disk snapshot before every change; never reset corrupt JSON.
        read_catalog()
        backup_catalog()
        write_atomic(CATALOG,json.dumps(data,ensure_ascii=False,indent=2))
        write_atomic(ROOT/'products.js','window.FORMA_PRODUCTS = '+json.dumps(data,ensure_ascii=False,separators=(',',':'))+';\n')

    def duplicate_product(self,body):
        pid=body.get('id')
        if type(pid) is not int:return self.json_response(400,{'error':'Некоректний товар'})
        with LOCK:
            catalog=read_catalog()
            source=next((p for p in catalog if p.get('id')==pid),None)
            if not source:return self.json_response(404,{'error':'Товар не знайдено'})
            fresh=next_product_id(catalog)
            result=dict(source,id=fresh,sku=f'FH-{fresh:04d}',slug=f'copy-{fresh}',
                        name=source['name']+' · копія',active=False,new=False,featured=False,
                        images=list(source.get('images',[])),stock=0)
            catalog.append(result);record_product_id(fresh);self.save_catalog_files(catalog)
        return self.json_response(200,{'ok':True,'product':result})


    def delete_product(self, body):
        # Explicitly confirmed destructive action; auth+CSRF is checked in do_POST.
        pid = body.get('id')
        confirm_sku = body.get('confirmSku')
        if type(pid) is not int or type(confirm_sku) is not str:
            return self.json_response(400, {'error':'Потрібно підтвердити артикул товару'})
        with LOCK:
            catalog = read_catalog()
            product = next((p for p in catalog if p.get('id') == pid), None)
            if product is None:
                return self.json_response(404, {'error':'Товар уже видалено або не знайдено'})
            if confirm_sku.strip() != product.get('sku'):
                return self.json_response(400, {'error':'Артикул не збігається. Товар не видалено.'})
            content = sitecms.read()
            collection_ids = [c['id'] for c in content.get('collections', []) if pid in c.get('productIds', [])]
            # Store a local rollback snapshot, without modifying the SQLite order history.
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup_file = BACKUP_DIR / f'deleted-product-{pid}-{int(time.time())}-{secrets.token_hex(3)}.json'
            write_atomic(backup_file, json.dumps({'product':product, 'collectionIds':collection_ids}, ensure_ascii=False, indent=2))
            # Never recycle the removed product ID/SKU for a later new product.
            record_product_id(max((p.get('id', 0) for p in catalog), default=pid))
            deleted = read_json(DELETE_LOG, [])
            if not isinstance(deleted, list): deleted=[]
            deleted.append({'id':pid, 'sku':product.get('sku',''), 'deletedAt':int(time.time())})
            write_atomic(DELETE_LOG, json.dumps(deleted, ensure_ascii=False, indent=2))
            revised = [p for p in catalog if p.get('id') != pid]
            self.save_catalog_files(revised)
            for collection in content.get('collections', []):
                collection['productIds'] = [x for x in collection.get('productIds', []) if x != pid]
            if collection_ids:
                sitecms.save(content)
        # Image files deliberately stay on disk: they can be shared or restored.
        return self.json_response(200, {'ok':True, 'deletedId':pid, 'nextSku':f'FH-{next_product_id(revised):04d}', 'removedFromCollections':collection_ids})

    def save_product(self,body):
        pid=body.get('id')
        changes=body.get('changes')
        if not isinstance(pid,int) or not isinstance(changes,dict) or not changes or any(k not in ADMIN_FIELDS for k in changes):
            return self.json_response(400,{'error':'Некоректні поля товару'})
        data=read_catalog()
        p=next((x for x in data if x['id']==pid),None)
        if p is None: return self.json_response(404,{'error':'Товар не знайдено'})
        try:
            for k,v in changes.items():
                if k in ('price','oldPrice','stock','width','depth','height'):
                    if v in (None,'') and k=='oldPrice': v=None
                    else:
                        if type(v) not in (int,float): raise ValueError('Числове поле '+k)
                        if not (0<=v<=10000 if k in ('width','depth','height') else 0<=v<=10000000): raise ValueError('Недопустиме значення '+k)
                        v=round(v,1) if k in ('width','depth','height') else int(v)
                elif k=='availableColors':
                    if not self.valid_colors(v): raise ValueError('Доступні кольори: не більше 16 назв по 40 символів')
                elif k in ('featured','new','active','allowCustomColor'):
                    if not isinstance(v,bool): raise ValueError('Прапорець '+k)
                else:
                    if not isinstance(v,str) or not (0 if k in ('manual','weight','series') else 1)<=len(v.strip())<=(10000 if k=='manual' else 2500): raise ValueError('Текстове поле '+k)
                    v=v.strip()
                p[k]=v
        except ValueError as e: return self.json_response(400,{'error':str(e)})
        if p.get('oldPrice') is not None and p['oldPrice'] <= p['price']:
            return self.json_response(400,{'error':'Стара ціна має бути вищою за ціну продажу'})
        with LOCK:
            self.save_catalog_files(data)
        return self.json_response(200,{'ok':True,'product':p})

    def save_photo(self,body):
        pid=body.get('id');index=body.get('index');source=body.get('data')
        if not isinstance(pid,int) or type(index) is not int or not 0<=index<24 or not isinstance(source,str):
            return self.json_response(400,{'error':'Неправильний формат фото'})
        found=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)',source)
        if not found: return self.json_response(400,{'error':'Підтримуються JPG, PNG, WebP'})
        try: raw=base64.b64decode(found.group(2),validate=True)
        except (ValueError,base64.binascii.Error): return self.json_response(400,{'error':'Некоректне зображення'})
        if len(raw)>5*1024*1024 or len(raw)<100: return self.json_response(413,{'error':'Фото: до 5 МБ'})
        mime=found.group(1); magic=raw[:16]
        valid=(mime=='jpeg' and magic.startswith(b'\xff\xd8\xff')) or (mime=='png' and magic.startswith(b'\x89PNG\r\n\x1a\n')) or (mime=='webp' and magic.startswith(b'RIFF') and magic[8:12]==b'WEBP')
        if not valid: return self.json_response(400,{'error':'Зображення не відповідає формату'})
        data=read_catalog()
        p=next((x for x in data if x['id']==pid),None)
        if p is None:return self.json_response(404,{'error':'Товар не знайдено'})
        if index>len(p.get('images',[])) or len(p.get('images',[]))>=24 and index==len(p['images']):
            return self.json_response(400,{'error':'Максимум 24 фотографії для одного товару'})
        ext={'jpeg':'jpg','png':'png','webp':'webp'}[mime]
        filename=f'uploaded-{pid:04d}-{index+1}-{secrets.token_hex(5)}.{ext}'
        IMAGE_DIR.mkdir(parents=True,exist_ok=True)
        (IMAGE_DIR/filename).write_bytes(raw)
        if index==len(p['images']): p['images'].append('assets/photos/'+filename)
        else: p['images'][index]='assets/photos/'+filename
        with LOCK:
            self.save_catalog_files(data)
        return self.json_response(200,{'ok':True,'url':p['images'][index]})

    def remove_photo(self,body):
        pid=body.get('id'); index=body.get('index')
        if type(pid) is not int or type(index) is not int:
            return self.json_response(400,{'error':'Некоректне фото'})
        with LOCK:
            data=read_catalog()
            p=next((x for x in data if x['id']==pid),None)
            if not p: return self.json_response(404,{'error':'Товар не знайдено'})
            ims=p.get('images',[])
            if not (len(ims)>1 and 0<=index<len(ims)):
                return self.json_response(400,{'error':'Потрібне хоча б одне фото'})
            ims.pop(index)
            self.save_catalog_files(data)
        return self.json_response(200,{'ok':True,'images':ims})


def main():
    os.chdir(ROOT)
    setup_password()
    loyalty.init_db()
    # Rebuild public metadata when upgrading an older site-content.json.
    content=sitecms.read()
    if 'categories' not in read_json(sitecms.DATA,{}):sitecms.save(content)
    port=int(os.environ.get('FORMA_PORT','8765'))
    host=os.environ.get('FORMA_HOST','127.0.0.1')
    try:
        server=ThreadingHTTPServer((host,port),Handler)
    except OSError as exc:
        print(f'ПОМИЛКА ЗАПУСКУ: порт {port} зайнятий або недоступний ({exc}).', flush=True)
        print('Закрийте попереднє вікно Forma Home або виконайте запуск на іншому порту.', flush=True)
        raise SystemExit(2)
    print(f'Forma Home доступний: http://127.0.0.1:{port}/')
    print(f'Захищена адмінпанель: http://127.0.0.1:{port}/admin')
    print('Натисніть Ctrl+C, щоб зупинити сервер. Дані зберігаються у папці проєкту.')
    try: webbrowser.open(f'http://127.0.0.1:{port}/')
    except Exception: pass
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nСервер зупинено')
    finally: server.server_close()

if __name__=='__main__': main()
