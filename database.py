import datetime
import sqlite3

DB_PATH = "nutrition_bot.db"


def _normalize(name: str) -> str:
    """Нормализует название продукта для сравнения в кэше."""
    return " ".join(name.lower().split())


# ---------------------------------------------------------------------------
# Инициализация и миграции
# ---------------------------------------------------------------------------

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
                carbs REAL,
                weight REAL DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS food_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_normalized TEXT UNIQUE,
                name_display TEXT,
                calories_100g REAL,
                p_100g REAL,
                f_100g REAL,
                c_100g REAL,
                last_used TEXT
            )
        """)
        conn.commit()

        # Безопасные миграции — игнорируем если колонка уже есть
        for sql in [
            "ALTER TABLE nutrition_logs ADD COLUMN weight REAL DEFAULT 0",
            "ALTER TABLE nutrition_logs ADD COLUMN is_deleted INTEGER DEFAULT 0",
        ]:
            try:
                cursor.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Записи о питании
# ---------------------------------------------------------------------------

def get_today_calories(user_id: int, date_str: str) -> float:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(calories) FROM nutrition_logs "
            "WHERE user_id = ? AND date = ? AND is_deleted = 0",
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
            "FROM nutrition_logs "
            "WHERE user_id = ? AND date = ? AND is_deleted = 0 ORDER BY id",
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
                 AND is_deleted = 0
                 ORDER BY id DESC LIMIT 1
               )""",
            (user_id, date_str, f"%{product_name}%"),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_food_by_id(food_id: int) -> bool:
    """Удаляет конкретную запись по первичному ключу (inline-кнопка ❌)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nutrition_logs WHERE id = ?", (food_id,))
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
            "AND is_deleted = 0 ORDER BY id DESC LIMIT 1"
        )
        if weight > 0:
            cursor.execute(
                f"UPDATE nutrition_logs "
                f"SET product_name=?, weight=?, calories=?, proteins=?, fats=?, carbs=? "
                f"WHERE id = ({sub})",
                (new_name, weight, calories, p, f, c, user_id, date_str, f"%{old_name}%"),
            )
        else:
            cursor.execute(
                f"UPDATE nutrition_logs "
                f"SET product_name=?, calories=?, proteins=?, fats=?, carbs=? "
                f"WHERE id = ({sub})",
                (new_name, calories, p, f, c, user_id, date_str, f"%{old_name}%"),
            )
        conn.commit()
        return cursor.rowcount > 0


def clear_today_foods(user_id: int, date_str: str) -> None:
    """Жёсткое удаление всех записей за день (для /clear_today)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM nutrition_logs WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Мягкое удаление и восстановление (inline-кнопка "Очистить день")
# ---------------------------------------------------------------------------

def soft_delete_today(user_id: int, date_str: str) -> int:
    """Помечает все активные записи за день как удалённые. Возвращает кол-во."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE nutrition_logs SET is_deleted = 1 "
            "WHERE user_id = ? AND date = ? AND is_deleted = 0",
            (user_id, date_str),
        )
        conn.commit()
        return cursor.rowcount


def restore_today(user_id: int, date_str: str) -> int:
    """Снимает флаг is_deleted с записей за день. Возвращает кол-во восстановленных."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE nutrition_logs SET is_deleted = 0 "
            "WHERE user_id = ? AND date = ? AND is_deleted = 1",
            (user_id, date_str),
        )
        conn.commit()
        return cursor.rowcount


def hard_delete_soft_deleted(user_id: int, date_str: str) -> None:
    """Окончательно удаляет мягко удалённые записи ("Оставить как есть")."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM nutrition_logs WHERE user_id = ? AND date = ? AND is_deleted = 1",
            (user_id, date_str),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Активность
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Кэш продуктов (экономия API-вызовов)
# ---------------------------------------------------------------------------

def find_in_cache(name: str) -> dict | None:
    """
    Ищет продукт в кэше по нормализованному имени (данные свежее 14 дней).
    Возвращает данные на 100г или None если не найдено.
    """
    normalized = _normalize(name)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT name_display, calories_100g, p_100g, f_100g, c_100g
               FROM food_cache
               WHERE name_normalized = ?
               AND julianday('now') - julianday(last_used) <= 14""",
            (normalized,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "name": row[0],
                "calories_100g": row[1],
                "p_100g": row[2],
                "f_100g": row[3],
                "c_100g": row[4],
            }
        return None


# ---------------------------------------------------------------------------
# Восстановление из Obsidian (после редеплоя)
# ---------------------------------------------------------------------------

def restore_from_obsidian(user_id: int, date_str: str, foods: list[dict]) -> int:
    """
    Восстанавливает записи дня из Obsidian в SQLite.
    Вызывается при старте только если БД для этого дня пуста.
    Возвращает количество восстановленных записей.
    """
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM nutrition_logs WHERE user_id = ? AND date = ? AND is_deleted = 0",
            (user_id, date_str),
        )
        if cursor.fetchone()[0] > 0:
            return 0  # данные уже есть — не перезаписываем

        for f in foods:
            cursor.execute(
                """INSERT INTO nutrition_logs
                   (user_id, date, meal_type, product_name, weight, calories, proteins, fats, carbs)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, date_str,
                    f.get("meal_type", "Перекус"),
                    f.get("name", ""),
                    float(f.get("weight", 0)),
                    float(f.get("calories", 0)),
                    float(f.get("p", 0)),
                    float(f.get("f", 0)),
                    float(f.get("c", 0)),
                ),
            )
        conn.commit()
        return len(foods)


def upsert_cache(items: list[dict]) -> None:
    """
    Сохраняет / обновляет данные на 100г для каждого продукта.
    Вызывается после каждого успешного log_food через AI.
    """
    today = datetime.date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for item in items:
            weight = float(item.get("weight", 0))
            if weight <= 0:
                continue
            name = item.get("name", "").strip()
            if not name:
                continue
            factor = 100.0 / weight
            cursor.execute(
                """INSERT INTO food_cache
                     (name_normalized, name_display, calories_100g, p_100g, f_100g, c_100g, last_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name_normalized) DO UPDATE SET
                     name_display  = excluded.name_display,
                     calories_100g = excluded.calories_100g,
                     p_100g        = excluded.p_100g,
                     f_100g        = excluded.f_100g,
                     c_100g        = excluded.c_100g,
                     last_used     = excluded.last_used""",
                (
                    _normalize(name),
                    name,
                    float(item.get("calories", 0)) * factor,
                    float(item.get("p", 0)) * factor,
                    float(item.get("f", 0)) * factor,
                    float(item.get("c", 0)) * factor,
                    today,
                ),
            )
        conn.commit()
