import sqlite3

DB_PATH = "nutrition_bot.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                daily_calorie_target INTEGER DEFAULT 2000,
                name TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nutrition_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                meal_type TEXT,
                product_name TEXT,
                calories REAL,
                proteins REAL,
                fats REAL,
                carbs REAL
            )
        """)
        conn.commit()


def get_or_create_user(user_id: int, name: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT daily_calorie_target FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute(
            "INSERT INTO users (user_id, name, daily_calorie_target) VALUES (?, ?, 2000)",
            (user_id, name)
        )
        conn.commit()
        return 2000


def get_today_calories(user_id: int, date_str: str) -> float:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(calories) FROM nutrition_logs WHERE user_id = ? AND date = ?",
            (user_id, date_str)
        )
        row = cursor.fetchone()
        return row[0] if row[0] is not None else 0.0


def log_food_to_db(user_id: int, date_str: str, meal_type: str, items: list[dict]):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for item in items:
            cursor.execute(
                """INSERT INTO nutrition_logs
                   (user_id, date, meal_type, product_name, calories, proteins, fats, carbs)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    date_str,
                    meal_type,
                    item.get("name", ""),
                    float(item.get("calories", 0)),
                    float(item.get("p", 0)),
                    float(item.get("f", 0)),
                    float(item.get("c", 0)),
                )
            )
        conn.commit()
