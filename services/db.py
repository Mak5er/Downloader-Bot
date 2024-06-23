import psycopg2

import config


class DataBase:

    def __init__(self):
        self.connect = psycopg2.connect(config.db_auth)
        self.cursor = self.connect.cursor()

    async def add_users(self, user_id, user_name, user_username, chat_type, language, status):
        try:
            with self.connect:
                self.cursor.execute(
                    """INSERT INTO users (user_id, user_name, user_username, chat_type, language, status) 
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING;""",
                    (user_id, user_name, user_username, chat_type, language, status))

        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def delete_user(self, user_id):
        try:
            with self.connect:
                self.cursor.execute(
                    "DELETE FROM users WHERE user_id = %s;",
                    (user_id,))
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def user_count(self):
        try:
            with self.connect:
                self.cursor.execute("SELECT COUNT(*) FROM users")
                return self.cursor.fetchone()[0]
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def active_user_count(self):
        try:
            with self.connect:
                self.cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'active'")
                return self.cursor.fetchone()[0]
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def inactive_user_count(self):
        try:
            with self.connect:
                self.cursor.execute("SELECT COUNT(*) FROM users WHERE status != 'active'")
                return self.cursor.fetchone()[0]
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def all_users(self):
        try:
            with self.connect:
                self.cursor.execute("SELECT user_id FROM users")
                return self.cursor.fetchall()

        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def user_exist(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                return self.cursor.fetchall()

        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def user_update_name(self, user_id, user_name, user_username):
        try:
            with self.connect:
                self.cursor.execute("UPDATE users SET user_username = %s, user_name = %s WHERE user_id = %s",
                                    (user_username, user_name, user_id))
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def get_user_captions(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("SELECT captions FROM users WHERE user_id = %s", (user_id,))
                return self.cursor.fetchone()[0]

        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def update_captions(self, captions, user_id):
        try:
            with self.connect:
                self.cursor.execute("UPDATE users SET captions = %s WHERE user_id = %s",
                                    (captions, user_id))
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def set_inactive(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("UPDATE users SET status = %s WHERE user_id = %s", ("inactive", user_id))
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def set_active(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("UPDATE users SET status = %s WHERE user_id = %s", ("active", user_id))
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def status(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("SELECT DISTINCT status FROM users WHERE user_id = %s", (user_id,))
                return self.cursor.fetchone()[0]
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def get_user_info(self, user_id):
        try:
            with self.connect:
                self.cursor.execute(
                    "SELECT user_name, user_username, status FROM users WHERE user_id = %s",
                    (user_id,))
                return self.cursor.fetchone()
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def get_user_info_username(self, user_username):
        try:
            with self.connect:
                self.cursor.execute(
                    "SELECT user_name, user_id, status FROM users WHERE user_username = %s",
                    (user_username,))
                return self.cursor.fetchone()
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def get_all_users_info(self):
        try:
            with self.connect:
                self.cursor.execute(
                    "SELECT user_id, chat_type, user_name, user_username, language, status, referrer_id FROM users")
                return self.cursor.fetchall()
        except psycopg2.OperationalError as e:
            print(e)
            pass

    async def ban_user(self, user_id):
        try:
            with self.connect:
                self.cursor.execute("UPDATE users SET status = %s WHERE user_id = %s", ("ban", user_id))
        except psycopg2.OperationalError as e:
            print(e)
            pass
