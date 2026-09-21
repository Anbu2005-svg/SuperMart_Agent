import os
from db.models import get_db_connection, init_db

SAMPLE_PRODUCTS = [
    {
        "sku_id": "SKU-ATTA-5K",
        "name": "Aashirvaad Whole Wheat Atta 5kg",
        "category": "Grains & Flour",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 210.0,
        "mrp": 245.0,
        "gst_slab": 5.0,
        "hsn_code": "1101",
        "quantity": 30.0,
        "reorder_level": 5.0
    },
    {
        "sku_id": "SKU-SALT-01",
        "name": "Tata Iodized Salt 1kg",
        "category": "Pantry Basics",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 20.0,
        "mrp": 28.0,
        "gst_slab": 0.0,
        "hsn_code": "2501",
        "quantity": 50.0,
        "reorder_level": 10.0
    },
    {
        "sku_id": "SKU-BUTTER-100",
        "name": "Amul Pasteurised Butter 100g",
        "category": "Dairy",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 52.0,
        "mrp": 62.0,
        "gst_slab": 12.0,
        "hsn_code": "0405",
        "quantity": 20.0,
        "reorder_level": 5.0
    },
    {
        "sku_id": "SKU-OIL-1L",
        "name": "Fortune Sunlite Sunflower Oil 1L",
        "category": "Edible Oils",
        "unit": "litre",
        "is_loose": False,
        "cost_price": 125.0,
        "mrp": 155.0,
        "gst_slab": 5.0,
        "hsn_code": "1512",
        "quantity": 40.0,
        "reorder_level": 8.0
    },
    {
        "sku_id": "SKU-MAGGI-70",
        "name": "Maggi 2-Minute Instant Noodles 70g",
        "category": "Snacks & Packaged Food",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 12.0,
        "mrp": 14.0,
        "gst_slab": 18.0,
        "hsn_code": "1902",
        "quantity": 100.0,
        "reorder_level": 20.0
    },
    {
        "sku_id": "SKU-PARLEG-80",
        "name": "Parle-G Gold Biscuits 80g",
        "category": "Snacks & Packaged Food",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 8.0,
        "mrp": 10.0,
        "gst_slab": 18.0,
        "hsn_code": "1905",
        "quantity": 80.0,
        "reorder_level": 15.0
    },
    {
        "sku_id": "SKU-SURF-1K",
        "name": "Surf Excel Easy Wash Detergent Powder 1kg",
        "category": "Household Care",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 115.0,
        "mrp": 140.0,
        "gst_slab": 18.0,
        "hsn_code": "3402",
        "quantity": 25.0,
        "reorder_level": 5.0
    },
    {
        "sku_id": "SKU-MILK-1L",
        "name": "Amul Taaza Toned Milk 1L",
        "category": "Dairy",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 54.0,
        "mrp": 56.0,
        "gst_slab": 0.0,
        "hsn_code": "0401",
        "quantity": 25.0,
        "reorder_level": 10.0
    },
    {
        "sku_id": "SKU-SUGAR-1K",
        "name": "Refined White Sugar 1kg (Loose)",
        "category": "Pantry Basics",
        "unit": "kg",
        "base_unit": "kg",
        "conversion_factor": 1.0,
        "is_loose": True,
        "cost_price": 40.0,
        "mrp": 48.0,
        "price_per_base_unit": 48.0,
        "gst_slab": 0.0,
        "hsn_code": "1701",
        "quantity": 60.0,
        "reorder_level": 15.0
    },
    {
        "sku_id": "SKU-RICE-1K",
        "name": "Basmati Rice 1kg (Loose)",
        "category": "Grains & Flour",
        "unit": "kg",
        "base_unit": "kg",
        "conversion_factor": 1.0,
        "is_loose": True,
        "cost_price": 65.0,
        "mrp": 80.0,
        "price_per_base_unit": 80.0,
        "gst_slab": 0.0,
        "hsn_code": "1006",
        "quantity": 50.0,
        "reorder_level": 10.0
    },
    {
        "sku_id": "SKU-DAL-1K",
        "name": "Toor Dal 1kg (Loose)",
        "category": "Grains & Flour",
        "unit": "kg",
        "base_unit": "kg",
        "conversion_factor": 1.0,
        "is_loose": True,
        "cost_price": 110.0,
        "mrp": 135.0,
        "price_per_base_unit": 135.0,
        "gst_slab": 0.0,
        "hsn_code": "0713",
        "quantity": 40.0,
        "reorder_level": 10.0
    },
    {
        "sku_id": "SKU-TEA-250",
        "name": "Brooke Bond Red Label Tea 250g",
        "category": "Beverages",
        "unit": "packet",
        "is_loose": False,
        "cost_price": 110.0,
        "mrp": 140.0,
        "gst_slab": 5.0,
        "hsn_code": "0902",
        "quantity": 15.0,
        "reorder_level": 5.0
    }
]

SAMPLE_CUSTOMERS = [
    {"name": "Ravi Kumar", "khata_balance": 0.0, "credit_limit": 2000.0, "phone": "+91-98xxxxxx01"},
    {"name": "Priya Sharma", "khata_balance": 250.0, "credit_limit": 500.0, "phone": "+91-98xxxxxx02"},
    {"name": "Suresh Patel", "khata_balance": 0.0, "credit_limit": 0.0, "phone": None}
]


def seed_database(db_path=None):
    """Initialize schema and seed PostgreSQL database with sample products and customers."""
    print("Initializing PostgreSQL schema...")
    init_db()
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Seed Products — upsert using ON CONFLICT
        for p in SAMPLE_PRODUCTS:
            row = dict(p)
            row.setdefault("base_unit", row.get("unit") or "piece")
            row.setdefault("conversion_factor", 1.0)
            row.setdefault("price_per_base_unit", row.get("mrp"))
            cur.execute("""
                INSERT INTO products (sku_id, name, category, unit, base_unit, conversion_factor, is_loose,
                                      cost_price, mrp, price_per_base_unit, gst_slab, hsn_code, quantity, reorder_level)
                VALUES (%(sku_id)s, %(name)s, %(category)s, %(unit)s, %(base_unit)s, %(conversion_factor)s, %(is_loose)s,
                        %(cost_price)s, %(mrp)s, %(price_per_base_unit)s, %(gst_slab)s, %(hsn_code)s, %(quantity)s, %(reorder_level)s)
                ON CONFLICT (sku_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    base_unit = EXCLUDED.base_unit,
                    conversion_factor = EXCLUDED.conversion_factor,
                    cost_price = EXCLUDED.cost_price,
                    mrp = EXCLUDED.mrp,
                    price_per_base_unit = EXCLUDED.price_per_base_unit,
                    gst_slab = EXCLUDED.gst_slab,
                    quantity = EXCLUDED.quantity,
                    reorder_level = EXCLUDED.reorder_level
            """, row)

        # Seed Customers — upsert
        for c in SAMPLE_CUSTOMERS:
            c_row = dict(c)
            c_row.setdefault("credit_limit", 0.0)
            c_row.setdefault("phone", None)
            cur.execute("""
                INSERT INTO customers (name, khata_balance, credit_limit, phone)
                VALUES (%(name)s, %(khata_balance)s, %(credit_limit)s, %(phone)s)
                ON CONFLICT (name) DO UPDATE SET
                    khata_balance = EXCLUDED.khata_balance,
                    credit_limit = EXCLUDED.credit_limit
            """, c_row)

        conn.commit()
        cur.close()
        print(f"PostgreSQL database seeded with {len(SAMPLE_PRODUCTS)} products and {len(SAMPLE_CUSTOMERS)} customers.")
    except Exception as e:
        conn.rollback()
        print(f"Seeding error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    seed_database()
