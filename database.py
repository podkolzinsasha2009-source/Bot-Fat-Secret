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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                description TEXT,
                burned_calories REAL
            )
        """)
        conn.commit()

        # Миграция: добавляем колонку weight в существующие БД (безопасно игнорирует если уже есть)
        try:
            cursor.execute("ALTER TABLE nutrition_logs ADD COLUMN weight REAL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Колонка уже существует


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
                   (user_id, date, meal_type, product_name, weight, calories, proteins, fats, carbs)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    date_str,
                    meal_type,
                    item.get("name", ""),
                    float(item.get("weight", 0)),
                    float(item.get("calories", 0)),
                    float(item.get("p", 0)),
                    float(item.get("f", 0)),
                    float(item.get("c", 0)),
                )
            )
        conn.commit()


def get_today_foods(user_id: int, date_str: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, meal_type, product_name, weight, calories, proteins, fats, carbs "
            "FROM nutrition_logs WHERE user_id = ? AND date = ? ORDER BY id",
            (user_id, date_str),
        )
        rows = cursor.fetchall()
        return [
            {"id": r[0], "meal_type": r[1], "name": r[2], "weight": r[3],
             "calories": r[4], "p": r[5], "f": r[6], "c": r[7]}
            for r in rows
        ]


def delete_food_from_db(user_id: int, date_str: str, product_name: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """DELETE FROM nutrition_logs WHERE id = (
                 SELECT id FROM nutrition_logs
                 WHERE user_id = ? AND date = ? AND LOWER(product_name) LIKE LOWER(?)
                 ORDER BY id DESC LIMIT 1
               )""",
            (user_id, date_str, f"%{product_name}%"),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_food_in_db(
    user_id: int, date_str: str,
    old_name: str, new_name: str,
    calories: float, p: float, f: float, c: float,
    weight: float = 0,
) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        sub = (
            "SELECT id FROM nutrition_logs "
            "WHERE user_id = ? AND date = ? AND LOWER(product_name) LIKE LOWER(?) "
            "ORDER BY id DESC LIMIT 1"
        )
        if weight > 0:
            cursor.execute(
                f"UPDATE nutrition_logs SET product_name=?, weight=?, calories=?, proteins=?, fats=?, carbs=? "
                f"WHERE id = ({sub})",
                (new_name, weight, calories, p, f, c, user_id, date_str, f"%{old_name}%"),
            )
        else:
            cursor.execute(
                f"UPDATE nutrition_logs SET product_name=?, calories=?, proteins=?, fats=?, carbs=? "
                f"WHERE id = ({sub})",
                (new_name, calories, p, f, c, user_id, date_str, f"%{old_name}%"),
            )
        conn.commit()
        return cursor.rowcount > 0


def clear_today_foods(user_id: int, date_str: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM nutrition_logs WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        conn.commit()


def log_activity_to_db(user_id: int, date_str: str, description: str, burned_calories: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_logs (user_id, date, description, burned_calories) VALUES (?, ?, ?, ?)",
            (user_id, date_str, description, float(burned_calories)),
        )
        conn.commit()


def get_today_burned(user_id: int, date_str: str) -> float:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(burned_calories) FROM activity_logs WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        row = cursor.fetchone()
        return row[0] if row[0] is not None else 0.0


def clear_today_activity(user_id: int, date_str: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM activity_logs WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        conn.commit()
