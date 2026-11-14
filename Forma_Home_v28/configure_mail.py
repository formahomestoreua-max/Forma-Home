"""Optional Windows-only secure setup for Gmail notifications (DPAPI)."""
import getpass,sys
import mailer

print('FORMA HOME — ПІДКЛЮЧЕННЯ ПОШТИ')
print('Адреса відправника:',mailer.FROM_ADDRESS)
print('Потрібен окремий 16-символьний пароль застосунку Google (не пароль від Gmail).')
print('Google Account → Безпека → Двоетапна перевірка → Паролі застосунків.')
print('Вхідні дані НЕ записуються у проєкт у відкритому вигляді.')
try:
    secret=getpass.getpass('Пароль застосунку (не показується): ')
    mailer.store_secret(secret)
    print('✓ Налаштування захищено Windows. Перезапустіть сервер Forma Home.')
    print('Перевірте стан пошти в адмінпанелі → Пошта й тарифи.')
except Exception as ex:
    print('ПОМИЛКА:',str(ex))
    sys.exit(1)
