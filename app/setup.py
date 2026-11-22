from __future__ import annotations

import argparse
import os
import sys

from app.db import apply_schema, get_connection
from app.reset import reset_demo
from app.seed import seed_demo_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.setup", description="Tek komutla kurulum")
    parser.add_argument("--reset", action="store_true", help="demo verisini tohuma döndür")
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL"):
        print("Hata: DATABASE_URL ortam değişkeni tanımlı değil.", file=sys.stderr)
        return 2

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('products')")
                exists = cur.fetchone()[0] is not None
                has_data = False
                if exists:
                    cur.execute("SELECT EXISTS (SELECT 1 FROM products)")
                    has_data = cur.fetchone()[0]
            if has_data and not args.reset:
                print("Zaten kurulu, veriye dokunulmadı (sıfırlamak için --reset).")
                return 0
            apply_schema(conn)
            conn.commit()
            if has_data:
                reset_demo(conn)
                print("Demo verisi tohuma döndürüldü.")
            else:
                with conn.transaction():
                    seed_demo_data(conn)
                print("Kurulum tamamlandı: şema uygulandı, tohum verisi yüklendi.")
    except Exception as exc:  # parola sızmasın diye yalnızca tür yazılır
        print(f"Hata: veritabanı işlemi başarısız ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
