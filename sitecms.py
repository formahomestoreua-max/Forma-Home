"""Local-only campaign / collection CMS with admin authentication enforced by server.py."""
import base64, binascii, json, re, secrets
from pathlib import Path

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'site-content.json'
IMAGES=ROOT/'assets'/'campaigns'

def read():
    try:
        value=json.loads(DATA.read_text(encoding='utf8'))
        if isinstance(value,dict):
            if 'categories' not in value:
                try:
                    products=json.loads((ROOT/'products.json').read_text(encoding='utf8'))
                    categories=sorted({p['category'] for p in products})
                    groups={'Меблі':('Дивани','Крісла','Ліжка','Столи','Стільці','Шафи','Комоди','Кухонні меблі'),
                      'Кухня':('Кухонні меблі','Стільці','Обідні столи'),
                      'Килими й освітлення':('Килими','Освітлення'),
                      'Декор і свято':('Новорічний декор','Гірлянди','Декор')}
                    value['categories']=[{'title':c,'group':next((g for g,ls in groups.items() if c in ls),'Інше'),'active':True,'image':''} for c in categories]
                except (OSError,ValueError,KeyError):value['categories']=[]
            return value
    except (OSError,ValueError): pass
    return {'banners':[],'collections':[],'categories':[]}

def save(value):
    from server import write_atomic
    write_atomic(DATA,json.dumps(value,ensure_ascii=False,indent=2))
    write_atomic(ROOT/'site-content.js','window.FORMA_SITE = '+json.dumps(value,ensure_ascii=False,separators=(',',':'))+';\n')

def safe_txt(v,maxlen=250):
    if not isinstance(v,str) or len(v.strip())>maxlen:return None
    return v.strip()

def post(h,path,body):
    if path=='/api/admin/site':
        kind=body.get('kind'); item=body.get('item'); remove=body.get('remove',False)
        key={'banner':'banners','collection':'collections'}.get(kind)
        if not key or not isinstance(item,dict):return h.json_response(400,{'error':'Невірний тип матеріалу'})
        iid=safe_txt(item.get('id'),80)
        if not iid or not re.fullmatch(r'[a-z0-9-]{2,80}',iid):return h.json_response(400,{'error':'ID має бути латиницею з дефісами'})
        content=read(); rows=content[key];existing=next((r for r in rows if r['id']==iid),None)
        if remove:
            content[key]=[r for r in rows if r['id']!=iid];save(content)
            return h.json_response(200,{'ok':True,'content':content})
        if len(rows)>=40 and not existing:return h.json_response(400,{'error':'Ліміт 40 елементів'})
        if kind=='banner':
            attrs=('eyebrow','title','description','button','link','image','style')
            maxlens=(100,120,350,60,160,240,30)
            r={k:safe_txt(item.get(k,''),n) for k,n in zip(attrs,maxlens)}
            if any(v is None for v in r.values()) or not r['title'] or not r['button'] or not r['link'].startswith('#/') or not r['image'].startswith('assets/') or r['style'] not in ('warm','forest','graphite'):
                return h.json_response(400,{'error':'Перевірте поля банера, фото і внутрішнє посилання #/...'})
        else:
            attrs=('title','subtitle','image');r={k:safe_txt(item.get(k,''),150 if k=='title' else 350) for k in attrs}
            if r.get('image') and (not r['image'].startswith('assets/') or '..' in r['image']):return h.json_response(400,{'error':'Некоректне фото колекції'})
            if any(v is None for v in r.values()) or not r['title']:return h.json_response(400,{'error':'Потрібна назва колекції'})
            ids=item.get('productIds',[])
            if not isinstance(ids,list) or len(ids)>500 or any(type(x) is not int or x<1 for x in ids):return h.json_response(400,{'error':'Некоректний список товарів'})
            # Validate against actual current catalog
            try:catalog=json.loads((ROOT/'products.json').read_text(encoding='utf8'))
            except Exception:catalog=[]
            known={p['id'] for p in catalog}
            r['productIds']=list(dict.fromkeys(x for x in ids if x in known))
        if type(item.get('active',True)) is not bool:return h.json_response(400,{'error':'Неправильна публікація'})
        r.update({'id':iid,'active':item.get('active',True)})
        if existing:rows[rows.index(existing)]=r
        else:rows.append(r)
        save(content)
        return h.json_response(200,{'ok':True,'content':content})
    if path=='/api/admin/banner-photo':
        raw=body.get('data','')
        if not isinstance(raw,str) or len(raw)>9*1024*1024:return h.json_response(400,{'error':'Надто велике фото'})
        m=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)',raw)
        if not m:return h.json_response(400,{'error':'Дозволені JPG, PNG, WebP'})
        try:data=base64.b64decode(m.group(2),validate=True)
        except (ValueError,binascii.Error):return h.json_response(400,{'error':'Не вдалося прочитати фото'})
        fmt=m.group(1)
        if not (500<len(data)<5*1024*1024) or not ((fmt=='jpeg' and data.startswith(b'\xff\xd8\xff')) or (fmt=='png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or (fmt=='webp' and data.startswith(b'RIFF') and data[8:12]==b'WEBP')):
            return h.json_response(400,{'error':'Некоректний файл або розмір більше 5 МБ'})
        IMAGES.mkdir(exist_ok=True,parents=True)
        name='campaign-'+secrets.token_hex(9)+'.'+{'jpeg':'jpg','png':'png','webp':'webp'}[fmt]
        (IMAGES/name).write_bytes(data)
        return h.json_response(200,{'ok':True,'url':'assets/campaigns/'+name})
    return h.json_response(404,{'error':'Невідомий маршрут'})
