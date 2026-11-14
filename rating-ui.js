/* Forma Home: accessible 1–5 star rating for reviews (server and legacy forms). */
(()=>{'use strict';
  const max = 5;
  function markup(){
    return `<fieldset class="fh-review-stars" aria-label="Оцінка товару від 1 до 5 зірок">
      <legend>Оцінка</legend>
      <div class="fh-review-stars-row">
        <div class="fh-star-picker" role="radiogroup" aria-label="Оберіть кількість зірок">
          ${Array.from({length:max},(_,i)=>`<label class="fh-star-option" data-star="${i+1}" title="${i+1} із 5">
            <input type="radio" name="stars" value="${i+1}" ${i===0?'required':''} aria-label="${i+1} із 5">
            <span class="fh-star-icon" aria-hidden="true">☆</span>
          </label>`).join('')}
        </div>
        <output class="fh-star-caption" aria-live="polite">Оберіть оцінку</output>
      </div>
      <p class="fh-star-hint">Натисніть на зірку — підсвітяться всі до обраної.</p>
    </fieldset>`;
  }
  function current(picker){return Number(picker.querySelector('input[name="stars"]:checked')?.value||0)}
  function draw(picker,hover=0){
    const selected=current(picker),value=hover||selected;
    picker.querySelectorAll('.fh-star-option').forEach(label=>{
      const n=Number(label.dataset.star),lit=n<=value;
      label.classList.toggle('is-lit',lit);
      label.querySelector('.fh-star-icon').textContent=lit?'★':'☆';
    });
    const caption=picker.parentElement?.querySelector('.fh-star-caption');
    if(caption)caption.textContent=hover?`${hover} із 5 — вибрати`:selected?`${selected} із 5`:'Оберіть оцінку';
  }
  document.addEventListener('change',event=>{
    if(event.target.matches('.fh-star-option input[name="stars"]'))draw(event.target.closest('.fh-star-picker'));
  });
  document.addEventListener('pointerover',event=>{
    if(event.pointerType==='touch')return;
    const label=event.target.closest('.fh-star-option');
    if(label)draw(label.closest('.fh-star-picker'),Number(label.dataset.star));
  });
  document.addEventListener('pointerout',event=>{
    if(event.pointerType==='touch')return;
    const picker=event.target.closest('.fh-star-picker');
    if(picker&&!picker.contains(event.relatedTarget))draw(picker);
  });
  document.addEventListener('focusin',event=>{
    const input=event.target.closest('.fh-star-option input[name="stars"]');
    if(input)draw(input.closest('.fh-star-picker'));
  });
  window.FormaRatingUI={markup};
})();
