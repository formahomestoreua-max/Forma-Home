(()=>{'use strict';
const q=s=>document.querySelector(s);
const escapeHTML=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let products=[],current=null,csrf='',collections=[],nextSku='';
async function reloadCollections(){const out=await api('/api/admin/content');collections=out.collections||[];window.FormaAdminCategories=out.categories||[];}
function collectionChoices(productId=null){return collections.length?collections.map(c=>`<label class="fh-pick-collection"><input type="checkbox" value="${escapeHTML(c.id)}" ${productId!==null&&(c.productIds||[]).includes(productId)?'checked':''}><span><strong>${escapeHTML(c.title)}</strong><small>${escapeHTML(c.subtitle||'Колекція товарів')}</small></span></label>`).join(''):'<p class="fh-no-collections">Колекцій ще немає. Створіть їх у розділі «Колекції товарів».</p>';}
function pickedCollections(container){return [...container.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value);}

const allCategoryNames=()=>[...new Set([...products.map(p=>p.category),...((window.FormaAdminCategories||[]).map(c=>c.title))])].sort((a,b)=>a.localeCompare(b,'uk'));
const listColors=s=>[...new Set(String(s||'').split(/[,;\n]/).map(x=>x.trim()).filter(Boolean))].slice(0,16);
const formFields=[['name','Назва товару','text'],['category','Категорія','text'],['price','Ціна, грн','number'],['oldPrice','Стара ціна, грн','number'],['stock','Залишок на складі','number'],['color','Колір','text'],['material','Матеріал','text'],['series','Серія / модель (необов’язково)','text'],['dimensions','Загальні габарити (Д × Ш × В), см','text'],['width','Ширина, см','number'],['depth','Глибина, см','number'],['height','Висота, см','number'],['weight','Вага (необов’язково)','text'],['warranty','Гарантія','text'],['assembly','Складання','text']];
async function api(url,method='GET',data){
 const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),15000);
 const opt={method,credentials:'same-origin',headers:{Accept:'application/json'},signal:ctrl.signal};
 if(data!==undefined){opt.headers['Content-Type']='application/json';opt.headers['X-FH-CSRF']=csrf;opt.body=JSON.stringify(data)}
 try{
  const response=await fetch(url,opt);
  // An old browser tab can outlive the Python server's session. Be explicit.
  if(response.status===401){if(!location.pathname.startsWith('/admin/login'))location.assign('/admin/login');throw Error('Сеанс завершився. Увійдіть в адмінпанель знову.')}
  const raw=await response.text();let result;
  try{result=JSON.parse(raw)}catch(_){throw Error('Сервер повернув не JSON. Перевірте адресу /admin і перезапустіть сторінку.')}
  if(!response.ok)throw Error(result.error||`Помилка сервера HTTP ${response.status}`);
  return result;
 }catch(error){
  if(error.name==='AbortError')throw Error('Сервер довго не відповідає. Перевірте CMD і повторіть запит.');
  if(error instanceof TypeError)throw Error('З’єднання із сервером втрачено. Перевірте, чи працює Python.');
  throw error;
 }finally{clearTimeout(timer)}
}

const currency=n=>Number(n).toLocaleString('uk-UA')+' ₴';
function status(message,err=false){const x=q('#status');x.textContent=message;x.classList.toggle('error',err)}
function stats(){q('#statAll').textContent=products.length;q('#statSale').textContent=products.filter(p=>p.oldPrice&&p.oldPrice>p.price).length;q('#statNew').textContent=products.filter(p=>p.new).length;q('#statCategories').textContent=new Set([...products.map(p=>p.category),...((window.FormaAdminCategories||[]).map(c=>c.title))]).size}
function listing(){const query=q('#search').value.trim().toLocaleLowerCase('uk');const visible=products.filter(p=>(p.name+' '+p.sku+' '+p.category).toLocaleLowerCase('uk').includes(query));q('#matchCount').textContent=`${visible.length} позицій`;q('#list').innerHTML=visible.map(p=>`<button class="product-row ${current===p.id?'selected':''}" data-id="${p.id}"><img src="${escapeHTML((p.images||[])[0]||'assets/photos/new-product-placeholder.svg')}" alt=""><span><b>${escapeHTML(p.name)}</b><small>${escapeHTML(p.sku)} · ${currency(p.price)} · ${p.stock||0} шт.${p.oldPrice&&p.oldPrice>p.price?' · АКЦІЯ':''}${p.active===false?' · ПРИХОВАНО':''}</small></span><span class="product-row-arrow">›</span></button>`).join('')||'<div class="no-match">Нічого не знайдено</div>'}
function render(){const p=products.find(p=>p.id===current);if(!p)return;q('#deleteProductTop').hidden=false;q('#editTitle').textContent=p.name;q('#editSku').textContent=p.sku+' · '+p.category;status('Готово до змін');q('#editor').innerHTML=`<form id="editForm"><div class="form-section-label"><b>Основні дані</b><span>Інформація у картці товару</span></div><div class="fields">${formFields.map(([key,label,type])=>`<label><span>${label}</span>${key==='category'?`<select name="category" required>${allCategoryNames().map(c=>`<option value="${escapeHTML(c)}" ${c===p.category?'selected':''}>${escapeHTML(c)}</option>`).join('')}</select>`:`<input name="${key}" type="${type}" ${type==='number'?'min="0" step="1"':''} value="${escapeHTML(p[key]??'')}">`}</label>`).join('')}<label class="wide"><span>Опис товару</span><textarea name="description" rows="5">${escapeHTML(p.description)}</textarea></label></div><div class="fh-spec-help">Заповнюй характеристики в будь-який час. Необов’язкові значення можна залишити порожніми — на сторінці товару вони не відображатимуться. Для моделі з кресленням введи точні розміри у відповідні поля.</div><div class="form-section-label"><b>Кольори на вибір</b><span>Вкажіть кольори через кому; вони з’являться у картці товару</span></div><div class="fh19-color-edit"><label>Доступні кольори<textarea name="availableColors" rows="2" placeholder="Молочний, Бежевий, Горіх">${escapeHTML((p.availableColors||[p.color]).filter(x=>x&&x!=='Уточнюється').join(', '))}</textarea></label><label class="fh19-check"><input name="allowCustomColor" type="checkbox" ${p.allowCustomColor?'checked':''}> Приймати індивідуальні побажання щодо кольору</label></div><div class="form-section-label"><b>Інструкція до товару</b><span>Текст збережеться на сервері та з’явиться у вкладці «Інструкція»</span></div><label class="manual-editor-label"><textarea name="manual" rows="7" maxlength="10000" placeholder="Напишіть покрокову інструкцію зі складання, догляду або використання. Кожен крок — з нового рядка.">${escapeHTML(p.manual||'')}</textarea></label><div class="form-flags"><label><input type="checkbox" name="featured" ${p.featured?'checked':''}> Вибір Forma Home</label><label><input type="checkbox" name="new" ${p.new?'checked':''}> Новинка</label><label><input type="checkbox" name="active" ${p.active!==false?'checked':''}> Опубліковано в каталозі</label></div><div class="promotion-hint"><strong>Акції:</strong> якщо «стара ціна» вища за поточну, сайт автоматично показує розмір знижки. Порожня стара ціна — без акції.</div><div class="form-section-label"><b>Колекції товару</b><span>Виберіть кілька добірок або зніміть галочки, щоб прибрати товар із колекції</span></div><div id="editCollections" class="fh-collection-picklist fh-pick-edit">${collectionChoices(p.id)}</div><div class="form-section-label photo-section-label"><b>Фотографії товару</b><span>Завантажуйте JPG, PNG або WebP до 5 МБ кожне</span></div><div class="photo-grid">${(p.images||[]).map((im,i)=>`<div class="photo-slot"><div class="photo-preview"><img src="${escapeHTML(im)}" alt="Ракурс ${i+1}"></div><label class="upload-btn">↥ Замінити фото ${i+1}<input type="file" data-photo="${i}" accept="image/jpeg,image/png,image/webp" hidden></label><button class="photo-remove" type="button" data-remove-photo="${i}" title="Видалити фото ${i+1}">Видалити</button></div>`).join('')}</div><div class="append-photo"><label class="append-photo-label">＋ Додати ще фотографії <input type="file" data-append-photos accept="image/jpeg,image/png,image/webp" multiple hidden></label><span>${(p.images||[]).length} із 24 фото · можна додати будь-яку кількість до ліміту</span></div><div class="form-bottom"><span>Зміни зберігаються у файлах <code>products.json</code> та <code>assets/photos</code> на цьому комп’ютері.</span><div class="fh19-editor-actions"><button type="button" class="fh19-delete-product" data-delete-product="${p.id}">Видалити товар</button><button type="button" id="duplicateProduct" data-duplicate-id="${p.id}" class="fh19-copy">Дублювати товар</button><button type="submit" class="primary">Зберегти товар →</button></div></div></form>`;
q('#editForm').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target),changes={};for(const [key,,type] of formFields){const val=f.get(key);changes[key]=type==='number'?(val===''&&key==='oldPrice'?null:Number(val||0)):String(val||'').trim()}changes.availableColors=listColors(f.get('availableColors'));changes.allowCustomColor=f.has('allowCustomColor');changes.description=String(f.get('description')).trim();changes.manual=String(f.get('manual')).trim();changes.featured=f.has('featured');changes.new=f.has('new');changes.active=f.has('active');const btn=e.target.querySelector('button[type=submit]');btn.disabled=true;status('Зберігаю...');try{const out=await api('/api/admin/product','POST',{id:p.id,changes});Object.assign(p,out.product);await api('/api/admin/product/collections','POST',{productId:p.id,collectionIds:pickedCollections(q('#editCollections'))});await reloadCollections();listing();stats();q('#editTitle').textContent=p.name;status('✓ Збережено')}catch(error){status(error.message,true)}finally{btn.disabled=false}});
q('#editor').querySelectorAll('[data-photo]').forEach(input=>input.addEventListener('change',async()=>{const file=input.files?.[0];if(!file)return;if(!['image/jpeg','image/png','image/webp'].includes(file.type))return status('Формат: JPG, PNG або WebP',true);if(file.size>5*1024*1024)return status('Зображення завелике (макс. 5 МБ)',true);status('Завантажую фото...');const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(Error('Не вдалося прочитати файл'));r.readAsDataURL(file)});try{const out=await api('/api/admin/photo','POST',{id:p.id,index:Number(input.dataset.photo),data});p.images[Number(input.dataset.photo)]=out.url;input.closest('.photo-slot').querySelector('img').src=out.url+'?v='+Date.now();listing();status('✓ Фото збережене в папці assets/photos')}catch(error){status(error.message,true)}}))}
function showEmptyCatalog(){
 q('#deleteProductTop').hidden=true;
 q('#editTitle').textContent='Каталог порожній';
 q('#editSku').textContent='Можна додати новий товар із новою категорією';
 q('#editor').innerHTML='<div class="fh-reliability-empty"><strong>У каталозі немає товарів</strong><p>Натисніть «＋ Додати товар у каталог» ліворуч, щоб створити перший товар. Артикул присвоїться автоматично.</p><button type="button" id="emptyCreateProduct">＋ Створити товар</button></div>';
 q('#emptyCreateProduct')?.addEventListener('click',()=>q('#createProduct').click());
}
async function initialize(){
 const recovery=q('#adminRecovery');
 if(recovery)recovery.hidden=true;
 status('Завантаження каталогу…');
 try{
  const ses=await api('/api/admin/session');csrf=ses.csrf;
  const data=await api('/api/admin/catalog');
  if(!Array.isArray(data.products))throw Error('Сервер повернув некоректний каталог. Дані не змінені.');
  products=data.products;nextSku=data.nextSku||'';
  let contentWarning='';
  try{await reloadCollections()}catch(error){contentWarning=' · Колекції тимчасово недоступні: '+error.message;collections=[];window.FormaAdminCategories=[];}
  stats();current=products.some(x=>x.id===current)?current:products[0]?.id??null;
  listing();if(current!==null)render();else showEmptyCatalog();
  status('✓ Каталог завантажено: '+products.length+' товарів'+contentWarning,Boolean(contentWarning));
 }catch(error){
  console.error('[Forma Home Admin] Не вдалося завантажити каталог',error);
  if(recovery){recovery.hidden=false;q('#adminRecoveryMessage').textContent=error.message;}
  q('#matchCount').textContent='Не вдалося завантажити';
  q('#list').innerHTML='<div class="fh-reliability-message">Каталог не завантажений. Спробуйте відновити з’єднання.</div>';
  q('#editor').innerHTML='<div class="fh-reliability-message">Дані товарів на сервері не видалені. Натисніть «Повторити завантаження».</div>';
  q('#deleteProductTop').hidden=true;
  status(error.message,true);
 }
}
q('#retryCatalog')?.addEventListener('click',initialize);

window.addEventListener('forma:categories-updated',async()=>{try{const data=await api('/api/admin/catalog');products=data.products;nextSku=data.nextSku||nextSku;listing();stats();render()}catch(err){status(err.message,true)}});
q('#search').addEventListener('input',listing);
q('#list').addEventListener('click',e=>{const b=e.target.closest('[data-id]');if(!b)return;current=Number(b.dataset.id);listing();render()});
const createDlg=q('#newProductDialog'),createForm=q('#newProductForm');
q('#createCategory').addEventListener('change',()=>{const isNew=q('#createCategory').value==='__new__';q('#createNewCategory').hidden=!isNew;q('#createNewCategory').required=isNew;if(isNew)q('#createNewCategory').focus()});
q('#createProduct').addEventListener('click',async()=>{try{await reloadCollections()}catch(err){collections=[];status('Колекції недоступні: '+err.message,true)}q('#createCollections').innerHTML=collectionChoices();const select=q('#createCategory');const cats=allCategoryNames();select.innerHTML='<option value="">Оберіть категорію</option>'+cats.map(c=>`<option value="${escapeHTML(c)}">${escapeHTML(c)}</option>`).join('')+'<option value="__new__">＋ Створити нову категорію</option>';q('#createNewCategory').hidden=Boolean(cats.length);q('#createNewCategory').required=!cats.length;if(!cats.length)select.value='__new__';const hint=q('#nextSkuHint');if(hint)hint.textContent=nextSku||'FH-'+String(Math.max(0,...products.map(x=>x.id))+1).padStart(4,'0');createDlg.showModal();createForm.elements.name.focus()});
function closeCreate(){createDlg.close();q('#newProductStatus').className='';q('#newProductStatus').textContent='';createForm.reset();q('#createNewCategory').hidden=true}
q('#closeCreate').addEventListener('click',closeCreate);q('#cancelCreate').addEventListener('click',closeCreate);
createDlg.addEventListener('click',e=>{if(e.target===createDlg)closeCreate()});
createForm.addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(createForm);const price=Number(f.get('price')),oldPrice=f.get('oldPrice')===''?null:Number(f.get('oldPrice'));if(oldPrice!==null&&oldPrice<=price){q('#newProductStatus').className='is-error';q('#newProductStatus').textContent='Стара ціна має бути вищою за ціну продажу';return;}
 const files=[...q('#createPhotos').files].slice(0,24);if(files.some(a=>a.size>5*1024*1024||!['image/jpeg','image/png','image/webp'].includes(a.type))){q('#newProductStatus').className='is-error';q('#newProductStatus').textContent='Підтримуються лише JPG/PNG/WebP до 5 МБ';return}
 const btn=q('#submitCreate');btn.disabled=true;q('#newProductStatus').className='';q('#newProductStatus').textContent='Створення товару…';try{
 const category=f.get('category')==='__new__'?String(f.get('customCategory')||'').trim():String(f.get('category')).trim();const res=await api('/api/admin/product/create','POST',{name:String(f.get('name')).trim(),category,price,oldPrice,stock:Number(f.get('stock')),featured:f.has('featured'),new:f.has('new'),active:f.has('active'),collectionIds:pickedCollections(q('#createCollections')),availableColors:listColors(f.get('availableColors')),allowCustomColor:f.has('allowCustomColor')});const p=res.product;products.push(p);current=p.id;nextSku='FH-'+String(p.id+1).padStart(4,'0');await reloadCollections();
 for(let i=0;i<files.length;i++){q('#newProductStatus').textContent=`Завантаження фото ${i+1} із ${files.length}…`;const file=files[i],data=await new Promise((resolve,reject)=>{const fr=new FileReader();fr.onload=()=>resolve(fr.result);fr.onerror=()=>reject(Error('Помилка читання фото'));fr.readAsDataURL(file)});const o=await api('/api/admin/photo','POST',{id:p.id,index:i,data});p.images[i]=o.url;}
 stats();listing();render();closeCreate();status('✓ Новий товар створено. Автоматичний артикул: '+p.sku);q('#editor').scrollIntoView({behavior:'smooth',block:'start'});
 }catch(ex){q('#newProductStatus').className='is-error';q('#newProductStatus').textContent='Помилка: '+ex.message+'. Якщо товар створено, його можна знайти після повторного завантаження.';status(ex.message,true);setTimeout(()=>initialize(),0)}finally{btn.disabled=false}});

async function asDataURL(file){return await new Promise((resolve,reject)=>{let r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(Error('Не вдалося прочитати фото'));r.readAsDataURL(file)})}
q('#editor').addEventListener('change',async e=>{
 const input=e.target.closest('[data-append-photos]');if(!input)return;
 const p=products.find(p=>p.id===current);if(!p)return;
 const files=[...input.files];if(p.images.length+files.length>24)return status('Можна мати щонайбільше 24 фотографії',true);
 if(files.some(f=>!['image/jpeg','image/png','image/webp'].includes(f.type)||f.size>5*1024*1024))return status('Фото мають бути JPG/PNG/WebP до 5 МБ кожне',true);
 input.disabled=true;
 try{for(let i=0;i<files.length;i++){status(`Додаю фото ${i+1} із ${files.length}…`);const out=await api('/api/admin/photo','POST',{id:p.id,index:p.images.length,data:await asDataURL(files[i])});p.images.push(out.url)};listing();render();status('✓ Додаткові фото збережено')}
 catch(ex){status(ex.message,true);render()}
});
q('#editor').addEventListener('click',async e=>{
 const copy=e.target.closest('[data-duplicate-id]');if(!copy)return;
 if(!confirm('Створити копію цього товару? Вона буде прихована до редагування і публікації.'))return;
 copy.disabled=true;try{const result=await api('/api/admin/product/duplicate','POST',{id:Number(copy.dataset.duplicateId)});products.push(result.product);current=result.product.id;listing();stats();render();status('✓ Створено копію '+result.product.sku+' — змініть назву, характеристики й фотографії перед публікацією')}catch(err){status(err.message,true);copy.disabled=false}
});
q('#editor').addEventListener('click',async e=>{
 const b=e.target.closest('[data-remove-photo]');if(!b)return;
 const p=products.find(p=>p.id===current);if(!p)return;
 const i=Number(b.dataset.removePhoto);
 if(p.images.length<=1)return status('Залиште хоча б одне фото',true);
 if(!confirm('Видалити це зображення з галереї?'))return;
 b.disabled=true;
 try{const out=await api('/api/admin/photo/remove','POST',{id:p.id,index:i});p.images=out.images;listing();render();status('✓ Фото вилучено з галереї')}
 catch(ex){status(ex.message,true);b.disabled=false}
});

const deleteDlg=q('#deleteProductDialog');
const deleteForm=q('#deleteProductForm');
let pendingDelete=null;
q('#editor').addEventListener('click',e=>{
 const button=e.target.closest('[data-delete-product]');if(!button)return;
 const product=products.find(p=>p.id===Number(button.dataset.deleteProduct));if(!product)return;
 pendingDelete=product;
 q('#deleteProductName').textContent=product.name;
 q('#deleteProductSku').textContent=product.sku;
 q('#deleteAction').disabled=false;
 q('#deleteCancel').disabled=false;
 q('#deleteProductError').textContent='';
 deleteDlg.showModal();
 q('#deleteCancel').focus();
});
function closeDelete(force=false){if(force||!q('#deleteAction').disabled){deleteDlg.close();pendingDelete=null;}}
q('#deleteProductTop').addEventListener('click',()=>{const button=q('#editor [data-delete-product]');if(button)button.click()});
q('#deleteCancel').addEventListener('click',()=>closeDelete());
deleteDlg.addEventListener('close',()=>{pendingDelete=null;q('#deleteCancel').disabled=false;});
deleteDlg.addEventListener('click',e=>{if(e.target===deleteDlg)closeDelete()});
deleteDlg.addEventListener('cancel',e=>{if(q('#deleteAction').disabled)e.preventDefault()});
deleteForm.addEventListener('submit',async e=>{
 e.preventDefault();if(!pendingDelete||q('#deleteAction').disabled)return;
 const item=pendingDelete;const button=q('#deleteAction');button.disabled=true;q('#deleteCancel').disabled=true;q('#deleteProductError').textContent='Видаляю…';
 try{
  const res=await api('/api/admin/product/delete','POST',{id:item.id,confirmSku:item.sku});
  products=products.filter(p=>p.id!==item.id);
  nextSku=res.nextSku||nextSku;
  try{await reloadCollections()}catch(error){console.warn('Колекції не оновлено:',error.message);}
  current=products[0]?.id??null;
  closeDelete(true);listing();stats();
  if(current!==null){render()}else{showEmptyCatalog()}
  status('✓ Товар '+item.sku+' видалено з каталогу та колекцій. Історія замовлень збережена.');
 }catch(err){q('#deleteProductError').textContent=err.message;button.disabled=false;q('#deleteCancel').disabled=false;status(err.message,true)}
});

q('#logout').addEventListener('click',async()=>{await api('/api/admin/logout','POST',{});location.href='/admin/login'});
initialize();
})();
