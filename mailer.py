"""Forma Home outbound transactional mail. Windows secrets are protected with DPAPI.
Mail is disabled until the owner configures their own Gmail application password.
No passwords are stored in JS, JSON, ZIP or the SQLite database.
"""
from __future__ import annotations
import ctypes, html, os, re, smtplib, ssl, threading, time
import bank_transfer
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FROM_ADDRESS='forma.home.store.ua@gmail.com'
SECRET_FILE=ROOT/'.forma_mail_secret.bin'
MAIL_LOCK=threading.Lock()


def _dpapi(blob:bytes, encrypt:bool)->bytes:
    if os.name!='nt':raise RuntimeError('DPAPI працює лише у Windows')
    from ctypes import wintypes
    class BLOB(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    source=ctypes.create_string_buffer(blob)
    source_blob=BLOB(len(blob),ctypes.cast(source,ctypes.POINTER(ctypes.c_ubyte)))
    result=BLOB()
    crypt=ctypes.windll.crypt32
    func=crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    if encrypt:
        success=func(ctypes.byref(source_blob),'Forma Home SMTP',None,None,None,0,ctypes.byref(result))
    else:
        success=func(ctypes.byref(source_blob),None,None,None,None,0,ctypes.byref(result))
    if not success:raise OSError(ctypes.get_last_error(),'Не вдалося обробити поштові налаштування Windows')
    try:return ctypes.string_at(result.data,result.size)
    finally:ctypes.windll.kernel32.LocalFree(result.data)


def password():
    env=os.environ.get('FORMA_SMTP_APP_PASSWORD','').replace(' ','').strip()
    if env:return env
    if os.name=='nt' and SECRET_FILE.exists():
        try:
            raw=SECRET_FILE.read_bytes()
            if raw.startswith(b'FHSMTP1'):
                return _dpapi(raw[7:],False).decode('utf-8')
        except Exception:pass
    return ''


def configured():return bool(password())


def store_secret(app_password):
    cleaned=re.sub(r'\s+','',app_password)
    if not re.fullmatch('[A-Za-z0-9]{16}',cleaned):
        raise ValueError('Потрібен 16-символьний пароль застосунку Google, не пароль від Gmail')
    if os.name!='nt':
        raise RuntimeError('На Linux/macOS використовуйте змінну середовища FORMA_SMTP_APP_PASSWORD')
    SECRET_FILE.write_bytes(b'FHSMTP1'+_dpapi(cleaned.encode('utf-8'),True))
    try:os.chmod(SECRET_FILE,0o600)
    except OSError:pass


def deliver(to,subject,plain,body_html=None):
    secret=password()
    if not secret:raise RuntimeError('Пошта не підключена: налаштуйте Gmail у власника сайту')
    msg=EmailMessage()
    msg['Subject']=subject
    msg['From']=formataddr(('Forma Home',FROM_ADDRESS))
    msg['To']=to
    msg['Reply-To']=FROM_ADDRESS
    msg.set_content(plain)
    if body_html:msg.add_alternative(body_html,subtype='html')
    with smtplib.SMTP_SSL('smtp.gmail.com',465,timeout=13,context=ssl.create_default_context()) as smtp:
        smtp.login(FROM_ADDRESS,secret)
        smtp.send_message(msg)


def queue(db,to,subject,plain,body_html='',order_id='',kind='order'):
    db.execute('''INSERT INTO email_queue(order_id,kind,to_email,subject,body_text,body_html,status,created_at)
      VALUES (?,?,?,?,?,?,'pending',?)''',(order_id,kind,to,subject,plain,body_html,int(time.time())))


def process_pending(connect,limit=12):
    if not configured():return {'ready':False,'sent':0}
    if not MAIL_LOCK.acquire(blocking=False):return {'ready':True,'sent':0,'busy':True}
    count=0
    try:
        with connect() as db:
            db.execute("UPDATE email_queue SET status='pending' WHERE status='sending'")
        with connect() as db:
            queue_rows=db.execute("SELECT * FROM email_queue WHERE status='pending' ORDER BY id LIMIT ?",(limit,)).fetchall()
        for item in queue_rows:
            with connect() as db:
                with db:db.execute("UPDATE email_queue SET status='sending',attempts=attempts+1 WHERE id=?",(item['id'],))
            try:
                deliver(item['to_email'],item['subject'],item['body_text'],item['body_html'] or None)
                with connect() as db:
                    with db:db.execute("UPDATE email_queue SET status='sent',sent_at=?,error='' WHERE id=?",(int(time.time()),item['id']))
                count+=1
            except Exception as err:
                # Never expose secrets or raw SMTP response text to the UI.
                clean='Не вдалося підключитися або авторизуватися у Gmail'
                if isinstance(err,ConnectionError) or isinstance(err,TimeoutError):clean='Немає зв’язку з поштовим сервером'
                with connect() as db:
                    with db:db.execute("UPDATE email_queue SET status='failed',error=? WHERE id=?",(clean,item['id']))
        return {'ready':True,'sent':count}
    finally:MAIL_LOCK.release()


def launch(connect):
    if configured():
        threading.Thread(target=process_pending,args=(connect,),daemon=True,name='FormaMail').start()


def status(connect):
    with connect() as db:
        rows=db.execute('SELECT status,COUNT(*) n FROM email_queue GROUP BY status').fetchall()
        latest=db.execute('SELECT id,to_email,subject,status,created_at,sent_at,error FROM email_queue ORDER BY id DESC LIMIT 30').fetchall()
    return {'email':FROM_ADDRESS,'configured':configured(), 'counts':{r['status']:r['n'] for r in rows},'messages':[dict(x) for x in latest]}


def _esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def _money(value):
    return f"{int(value):,}".replace(',', ' ') + ' ₴'


def confirmation_markup(db, order_id, kind='created', printable=False):
    # Branded, printable order confirmation. NOT a fiscal receipt.
    # Caller must check authorization before exposing it as a web response.
    from datetime import datetime
    o=db.execute('''SELECT o.*, c.name client_name, c.email client_email
        FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?''', (order_id,)).fetchone()
    if not o:
        return ''
    items=db.execute('SELECT title,quantity,unit_price,color_choice FROM order_items WHERE order_id=?',(order_id,)).fetchall()
    labels={'new':'Очікує підтвердження','accepted':'Прийнято','processing':'Комплектується',
        'scheduled':'Доставку заплановано','in_delivery':'У дорозі','fulfilled':'Виконано','cancelled':'Скасовано'}
    delivery_names={'own_standard':'Стандартна доставка Forma Home',
        'own_large':'Великогабаритна доставка','own_priority':'Пріоритетна доставка'}
    payment_names={'cod':'Оплата при отриманні','invoice':'Банківський переказ за рахунком',
        'online':'Онлайн-оплата через платіжний сервіс'}
    state=('В обробці · перевірка переказу' if o['payment']=='invoice' and o['payment_status']!='paid' and o['status']!='cancelled' else ('Оплату підтверджено' if o['payment']=='invoice' and o['payment_status']=='paid' and o['status'] in ('new','accepted','processing') else labels.get(o['status'], 'Обробляється')))
    paystate='Оплату підтверджено' if o['payment_status']=='paid' else 'В обробці — очікує перевірки переказу' if o['payment']=='invoice' else 'Оплата при отриманні'
    deliver_date=_esc(o['delivery_date'] or 'Уточнюється менеджером')
    deliver_time=_esc(o['delivery_window'] or 'Погоджується окремо')
    created=datetime.fromtimestamp(o['created_at']).strftime('%d.%m.%Y, %H:%M')
    small='font-size:12px;color:#7d7a76;line-height:1.5'
    row_style='padding:15px 0;border-bottom:1px solid #e8e2d9;vertical-align:top'
    item_lines=''.join(
        f'<tr><td style="{row_style};font-size:14px;font-weight:600;color:#282724">{_esc(i["title"])}'
        f'<div style="font-size:12px;color:#8b8176;font-weight:400;margin-top:4px">{i["quantity"]} шт. × {_money(i["unit_price"])} · Колір: {_esc(i["color_choice"] or "не вказано")}</div></td>'
        f'<td style="{row_style};font-size:14px;font-weight:700;color:#282724;text-align:right;white-space:nowrap">{_money(i["quantity"]*i["unit_price"])}</td></tr>'
        for i in items)
    line_items=[('Товари',_money(o['subtotal'])),('Доставка',_money(o['delivery_cost'])),
        ('Складання меблів',_money(o['assembly_cost'])),('Використано бонусів','− '+_money(o['bonus_used']))]
    totals=''.join(f'<tr><td style="padding:7px 0;font-size:13px;color:#756d63">{label}</td><td style="padding:7px 0;font-size:13px;font-weight:600;color:#3b332a;text-align:right">{val}</td></tr>' for label,val in line_items)
    note=(f'<div style="margin-top:18px;padding:14px 18px;background:#f6f4ef;border-radius:12px;font-size:13px;color:#645d55">'
          f'<b>Коментар менеджера</b><p style="margin:7px 0 0;white-space:pre-wrap">{_esc(o["manager_note"])}</p></div>') if o['manager_note'] else ''
    bank_block=''
    if o['payment']=='invoice':
        info=bank_transfer.details(order_id,o['total'])
        bank_block=(f'<div style="background:#f8f5ef;border:1px solid #e6d8c5;border-radius:12px;padding:20px;margin:20px 0">'
          f'<div style="font-size:12px;letter-spacing:1.2px;color:#97774b;font-weight:700;margin-bottom:12px">РЕКВІЗИТИ ДЛЯ ПЕРЕКАЗУ</div>'
          f'<p style="font-size:13px;margin:6px 0"><b>Отримувач:</b> {_esc(info["recipient"])}</p>'
          f'<p style="font-size:13px;margin:6px 0;word-break:break-all"><b>IBAN:</b> {_esc(info["iban"])}</p>'
          f'<p style="font-size:13px;margin:6px 0"><b>ІПН / ЄДРПОУ:</b> {_esc(info["taxId"])}</p>'
          f'<p style="font-size:13px;margin:6px 0"><b>Призначення:</b> {_esc(info["purpose"])}</p>'
          f'<p style="font-size:13px;margin:6px 0"><b>Сума:</b> {_money(o["total"])}</p>'
          f'<p style="font-size:12px;color:#776c5b;margin:12px 0 0">Статус: {_esc(paystate)}. Після перевірки надходження в банку менеджер оновить статус.</p></div>')
    actions=('<div class="hide-print" style="text-align:center;padding:22px 16px">'
             '<button onclick="window.print()" style="padding:13px 22px;border:0;border-radius:10px;background:#2c3b35;color:white;font:600 14px Arial,sans-serif;cursor:pointer">'
             'Друкувати / зберегти як PDF</button></div>') if printable else ''
    css='''<style>
     @page{size:A4;margin:14mm}
     @media screen and (max-width:640px){.receipt-wrap{padding:12px!important}.receipt-sheet{padding:24px 20px!important}.receipt-meta{display:block!important}.receipt-meta>td{display:block!important;width:100%!important;padding-bottom:10px!important}}
     @media print{body{background:#fff!important}.receipt-wrap{padding:0!important}.receipt-sheet{box-shadow:none!important;border:none!important;border-radius:0!important;padding:0!important}.hide-print{display:none!important}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
    </style>'''
    return f'''<!doctype html><html lang="uk"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forma Home — замовлення {_esc(order_id)}</title>{css}</head>
    <body style="margin:0;background:#eeeae4;color:#2d2b28;font-family:Arial,Helvetica,sans-serif">
    <div class="receipt-wrap" style="max-width:760px;margin:0 auto;padding:30px 14px">
    <main class="receipt-sheet" style="background:#fff;border:1px solid #e4ded4;border-radius:18px;box-shadow:0 12px 38px #211c1820;padding:42px 45px">
    <table role="presentation" style="width:100%;border-collapse:collapse"><tr><td>
     <div style="font-size:26px;letter-spacing:5px;font-weight:600;color:#2c3b35">FORMA<span style="font-weight:400;color:#a07954"> HOME</span></div>
     <div style="letter-spacing:3px;font-size:9px;color:#91867b;margin-top:6px">FURNITURE &amp; HOME INTERIORS</div>
    </td><td style="text-align:right;vertical-align:top;font-size:11px;color:#82786b">ОФОРМЛЕНО<br><b style="color:#3d392f">{_esc(created)}</b></td></tr></table>
    <div style="border-top:1px solid #e8e2d9;margin:27px 0 30px"></div>
    <div style="color:#a27c55;letter-spacing:2.2px;font-weight:700;font-size:11px">ВАШЕ ЗАМОВЛЕННЯ</div>
    <h1 style="font-size:29px;line-height:1.2;font-weight:600;letter-spacing:-.8px;margin:10px 0 8px">Підтвердження замовлення</h1>
    <div style="font-size:14px;color:#716a62;margin-bottom:19px">№ <b style="color:#2c3b35">{_esc(order_id)}</b></div>
    <div style="background:#f1f4ef;border-left:4px solid #658273;border-radius:8px;padding:14px 18px;margin-bottom:25px">
       <div style="font-size:12px;color:#65766c">Статус замовлення</div>
       <strong style="font-size:17px;color:#2c4838;display:block;margin:5px 0">{_esc(state)}</strong>
       <div style="font-size:12px;color:#59695f">Оплата: {_esc(paystate)}</div>
    </div>
    <table role="presentation" style="width:100%;border-collapse:collapse"><tr class="receipt-meta">
      <td style="width:50%;vertical-align:top;padding:0 16px 20px 0"><div style="{small}">ПОКУПЕЦЬ</div><b style="display:block;margin:6px 0;font-size:14px">{_esc(o['client_name'])}</b><span style="font-size:12px;color:#6b6863">{_esc(o['client_email'])}</span></td>
      <td style="width:50%;vertical-align:top;padding:0 0 20px"><div style="{small}">ДОСТАВКА</div><b style="display:block;margin:6px 0;font-size:14px">{_esc(delivery_names.get(o['delivery'],'Доставка Forma Home'))}</b><span style="font-size:12px;color:#6b6863">{_esc(o['city'])}, {_esc(o['address'])}</span></td>
    </tr></table>
    <div style="padding:12px 17px;background:#faf8f5;border:1px solid #eae4dc;border-radius:10px;margin-bottom:21px;font-size:12px;color:#6c645c">
     <strong style="color:#423a34">Дата доставки:</strong> {deliver_date} <span style="color:#c4bdb1"> &nbsp;•&nbsp; </span><strong style="color:#423a34">Інтервал:</strong> {deliver_time}<br>
     <strong style="color:#423a34">Спосіб оплати:</strong> {_esc(payment_names.get(o['payment'],o['payment']))}
    </div>
    <h2 style="font-size:17px;font-weight:600;margin:28px 0 8px">Склад замовлення</h2>
    <table role="presentation" style="width:100%;border-collapse:collapse">{item_lines}</table>
    <table role="presentation" style="width:100%;border-collapse:collapse;margin-top:20px">{totals}</table>
    <table role="presentation" style="width:100%;border-collapse:collapse;background:#f7f3ed;border-radius:12px;margin-top:16px"><tr>
      <td style="padding:19px;font-size:16px;font-weight:700;color:#302c28">Разом до оплати</td>
      <td style="padding:19px;font-size:25px;text-align:right;color:#283b34;font-weight:700;white-space:nowrap">{_money(o['total'])}</td>
    </tr></table>
    {bank_block}
    {note}
    <p style="font-size:11px;color:#8b8175;line-height:1.6;margin:25px 0 0">Це інформаційний документ, не фіскальний чек. Статус оплати оновлюється менеджером після перевірки надходження коштів. Після оплати продавець надає розрахунковий документ згідно з чинним законодавством.</p>
    <div style="border-top:1px solid #e8e2d9;margin-top:24px;padding-top:19px;text-align:center"><div style="letter-spacing:1px;font-size:11px;font-weight:700;color:#3b463e">ДЯКУЄМО, ЩО ОБИРАЄТЕ FORMA HOME</div><div style="font-size:12px;margin-top:8px"><a style="color:#9a744d" href="mailto:{FROM_ADDRESS}">{FROM_ADDRESS}</a></div></div>
    </main>{actions}</div></body></html>'''


def receipt(db,order_id,kind='created'):
    o=db.execute('''SELECT o.*,c.name client_name,c.email client_email FROM orders o
      JOIN customers c ON c.id=o.customer_id WHERE o.id=?''',(order_id,)).fetchone()
    if not o:return
    items=db.execute('SELECT title,quantity,unit_price,color_choice FROM order_items WHERE order_id=?',(order_id,)).fetchall()
    labels={'new':'Очікує підтвердження','accepted':'Прийнято','processing':'Комплектується',
       'scheduled':'Доставку заплановано','in_delivery':'У дорозі','fulfilled':'Виконано','cancelled':'Скасовано'}
    subject=('Forma Home · Підтвердження '+order_id) if kind=='created' else ('Forma Home · Статус замовлення '+order_id)
    lines=[f'Forma Home — замовлення № {order_id}',f'Вітаємо, {o["client_name"]}!',
       f'Статус: {labels.get(o["status"],o["status"])}',
       f'Оплата: {"Оплачено (перевірено менеджером)" if o["payment_status"]=="paid" else "В обробці — очікує перевірки"}',
       f'Дата доставки: {o["delivery_date"] or "Узгоджується"}',
       f'Час доставки: {o["delivery_window"] or "Узгоджується"}', '', 'Товари:']
    for item in items:
        lines.append(f'• {item["title"]} · Колір: {item["color_choice"] or "уточнюється"} — {item["quantity"]} × {_money(item["unit_price"])}')
    lines += ['', f'Товари: {_money(o["subtotal"])}', f'Доставка: {_money(o["delivery_cost"])}',
              f'Складання: {_money(o["assembly_cost"])}',f'Бонуси: −{_money(o["bonus_used"])}',
              f'До оплати: {_money(o["total"])}','',
              'Документ підтверджує замовлення, не є фіскальним чеком.',
              'Контакти: '+FROM_ADDRESS]
    if o['payment']=='invoice':
        bank=bank_transfer.details(order_id,o['total'])
        lines += ['', 'Реквізити для переказу:',
                  'Отримувач: '+bank['recipient'], 'IBAN: '+bank['iban'],
                  'ІПН/ЄДРПОУ: '+bank['taxId'], 'Призначення: '+bank['purpose']]
    plain='\n'.join(lines)
    markup=confirmation_markup(db,order_id,kind,printable=False)
    queue(db,o['client_email'],subject,plain,markup,order_id,kind)
