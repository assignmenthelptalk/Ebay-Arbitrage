import sqlite3, json, datetime
conn = sqlite3.connect('/root/arbitrage-api/arbitrage.db')
conn.execute("DELETE FROM orders WHERE order_id = 'DRY-RUN-014'")
addr = json.dumps({"full_name": "Susan Murimi", "line1": "1 Infinite Loop", "city": "Cupertino", "state": "CA", "postcode": "95014"})
conn.execute(
    "INSERT INTO orders (order_id, amazon_asin, item_title, fulfillment_status, shipping_address, sale_price, quantity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ('DRY-RUN-014', 'B07ZVKTP53', 'Anker USB Hub', 'pending', addr, 39.99, 1, datetime.datetime.now(datetime.timezone.utc).isoformat())
)
conn.commit()
conn.close()
print('Order DRY-RUN-014 inserted')
