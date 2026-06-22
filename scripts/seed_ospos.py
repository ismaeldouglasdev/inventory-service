#!/usr/bin/env python3
"""Popula ospos_items com produtos de teste (categorias propositalmente inconsistentes)."""

import asyncio
import aiomysql

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "admin",
    "password": "pointofsale",
    "db": "ospos",
}

PRODUTOS = [
    # (name, description, category, unit_price, stock)
    ("Caneca Personalizada", "Caneca de cerâmica 300ml", "Utilidade", 29.90, 50),
    ("Caneca Térmica", "Caneca inox com tampa", "UTILIDADES", 49.90, 30),
    ("Porta-retrato 10x15", "Porta-retrato de madeira", "utilidades", 19.90, 40),
    ("Camiseta Básica Preta", "Camiseta 100% algodão", "Vestuário", 59.90, 100),
    ("Camiseta Básica Branca", "Camiseta 100% algodão", "VESTUARIO", 59.90, 80),
    ("Boné Aba Curva", "Boné pré-curvado", "Vestuario", 39.90, 60),
    ("Mousepad Gamer", "Mousepad grande 90x40cm", "Informática", 79.90, 25),
    ("Teclado Mecânico", "Teclado RGB switch blue", "INFORMATICA", 199.90, 15),
    ("Hub USB 4 portas", "Hub USB 3.0", "informatica", 34.90, 35),
    ("Chaveiro Personalizado", "Chaveiro em acrílico", "Diversos", 9.90, 200),
    ("Ímã de Geladeira", "Ímã decorativo 5cm", "DIVERSOS", 5.90, 150),
    ("Squeeze Academia", "Garrafa 500ml", "diversos", 24.90, 45),
    ("Almofada Decorativa", "Almofada 45cm xadrez", "Casa", 49.90, 20),
    ("Jogo de Panelas", "Panelas antiaderentes 4 peças", "CASA", 149.90, 10),
    ("Tapete de Sala", "Tapete 1,50m x 2m", "casa", 89.90, 8),
]

async def seed():
    pool = await aiomysql.create_pool(**DB_CONFIG, autocommit=True)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Limpa dados existentes
            await cur.execute("DELETE FROM ospos_items")
            await cur.execute("DELETE FROM ospos_item_quantities")
            await cur.execute("DELETE FROM ospos_inventory")

            for name, desc, cat, price, stock in PRODUTOS:
                await cur.execute(
                    """INSERT INTO ospos_items
                       (name, description, category, unit_price, cost_price,
                        receiving_quantity, deleted, allow_alt_description, is_serialized)
                       VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 0)""",
                    (name, desc, cat, price, price * 0.6, stock),
                )
                item_id = cur.lastrowid
                await cur.execute(
                    "INSERT INTO ospos_item_quantities (item_id, location_id, quantity) VALUES (%s, 1, %s)",
                    (item_id, stock),
                )
    pool.close()
    await pool.wait_closed()
    print(f"✅ {len(PRODUTOS)} produtos inseridos com categorias bagunçadas!")


if __name__ == "__main__":
    asyncio.run(seed())

