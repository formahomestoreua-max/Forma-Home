"""Forma Home — реквізити для IBAN-переказів.
Віддаються покупцеві лише для його власного замовлення через захищений маршрут.
Перед публічним запуском перевірте тип рахунку та його придатність для торгівлі.
"""
RECIPIENT = "Ганенко Рєнат Вікторович"
IBAN = "UA693220010000026207336237947"
TAX_ID = "3967004731"
BASE_PURPOSE = "Поповнення рахунку"

def details(order_id: str, amount: int) -> dict:
    return {
        "recipient": RECIPIENT,
        "iban": IBAN,
        "taxId": TAX_ID,
        "purpose": f"{BASE_PURPOSE}. Замовлення {order_id}",
        "orderId": order_id,
        "amount": int(amount),
        "currency": "UAH"
    }
