"""
Tests for Single-Table Inheritance (STI).

# hyper-test: db_isolated

Tests:
- Parent and child share same table
- Child save() auto-sets discriminator
- Child QuerySet auto-filters by discriminator
- Parent QuerySet returns all rows
- Child-specific fields work
- Multiple child types on same table
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


# --- Models ---


class Vehicle(Model):
    class Meta:
        table = "sti_vehicles"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    type: str = Field(default="vehicle")
    top_speed: int = Field(default=0)


class Car(Vehicle):
    class Meta:
        sti = True
        sti_type = "car"

    doors: int = Field(default=4)


class Truck(Vehicle):
    class Meta:
        sti = True
        sti_type = "truck"

    payload_tons: int = Field(default=0)


async def main():
    print("=" * 60)
    print("Single-Table Inheritance (STI) Tests")
    print("=" * 60)

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Setup — single table with all columns
    await db.execute("DROP TABLE IF EXISTS sti_vehicles CASCADE")
    await db.execute("""
        CREATE TABLE sti_vehicles (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'vehicle',
            top_speed INTEGER NOT NULL DEFAULT 0,
            doors INTEGER DEFAULT 4,
            payload_tons INTEGER DEFAULT 0
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sti_type ON sti_vehicles (type)")

    # === Metadata ===
    print("\n--- Metadata ---")
    check(
        "Car shares Vehicle table",
        Car._meta.table == "sti_vehicles",
        f"got {Car._meta.table}",
    )
    check(
        "Truck shares Vehicle table",
        Truck._meta.table == "sti_vehicles",
        f"got {Truck._meta.table}",
    )
    check("Car sti_type is 'car'", Car._meta.sti_type == "car")
    check("Truck sti_type is 'truck'", Truck._meta.sti_type == "truck")
    check("Car sti_column is 'type'", Car._meta.sti_column == "type")
    check("Car has 'doors' field", "doors" in Car._meta.fields)
    check("Truck has 'payload_tons' field", "payload_tons" in Truck._meta.fields)

    # === Create via child models ===
    print("\n--- Create ---")
    car1 = Car(name="Sedan", top_speed=200, doors=4)
    await car1.save()
    check("Car saved with id", car1.id is not None, f"id={car1.id}")

    car2 = Car(name="Coupe", top_speed=250, doors=2)
    await car2.save()
    check("Car2 saved", car2.id is not None)

    truck1 = Truck(name="Hauler", top_speed=120, payload_tons=10)
    await truck1.save()
    check("Truck saved", truck1.id is not None)

    # Also insert a plain Vehicle directly
    await db.execute(
        "INSERT INTO sti_vehicles (name, type, top_speed) VALUES ($1, $2, $3)",
        "Bicycle",
        "vehicle",
        30,
    )

    # === Discriminator auto-set ===
    print("\n--- Discriminator ---")
    row = await db.query_one("SELECT type FROM sti_vehicles WHERE id = $1", car1.id)
    check("Car discriminator is 'car'", row["type"] == "car", f"got {row['type']}")

    row = await db.query_one("SELECT type FROM sti_vehicles WHERE id = $1", truck1.id)
    check(
        "Truck discriminator is 'truck'", row["type"] == "truck", f"got {row['type']}"
    )

    # === Child QuerySet filters by discriminator ===
    print("\n--- Child QuerySet auto-filter ---")
    cars = await Car.objects.all()
    check(
        "Car.objects.all() → 2 cars",
        len(cars) == 2,
        f"got {len(cars)}: {[c.name for c in cars]}",
    )

    trucks = await Truck.objects.all()
    check(
        "Truck.objects.all() → 1 truck",
        len(trucks) == 1,
        f"got {len(trucks)}: {[t.name for t in trucks]}",
    )

    # === Parent QuerySet returns all rows ===
    print("\n--- Parent QuerySet ---")
    all_vehicles = await Vehicle.objects.all()
    check(
        "Vehicle.objects.all() → 4 total",
        len(all_vehicles) == 4,
        f"got {len(all_vehicles)}: {[v.name for v in all_vehicles]}",
    )

    # === Child-specific field values ===
    print("\n--- Child fields ---")
    coupe = await Car.objects.filter(name="Coupe").first()
    check(
        "Coupe doors=2",
        coupe is not None and coupe.doors == 2,
        f"got {coupe.doors if coupe else 'None'}",
    )

    hauler = await Truck.objects.filter(name="Hauler").first()
    check(
        "Hauler payload=10",
        hauler is not None and hauler.payload_tons == 10,
        f"got {hauler.payload_tons if hauler else 'None'}",
    )

    # === Count ===
    print("\n--- Count ---")
    car_count = await Car.objects.count()
    check("Car count = 2", car_count == 2, f"got {car_count}")

    truck_count = await Truck.objects.count()
    check("Truck count = 1", truck_count == 1, f"got {truck_count}")

    all_count = await Vehicle.objects.count()
    check("Vehicle count = 4", all_count == 4, f"got {all_count}")

    # === Filter with lookups on child ===
    print("\n--- Lookups ---")
    fast_cars = await Car.objects.filter(top_speed__gte=220).all()
    check("Fast cars (>=220) → 1 (Coupe)", len(fast_cars) == 1, f"got {len(fast_cars)}")
    if fast_cars:
        check("Fast car is Coupe", fast_cars[0].name == "Coupe")

    # === Order by on child ===
    ordered = await Car.objects.order_by("-top_speed").all()
    check(
        "Cars ordered by speed desc",
        [c.name for c in ordered] == ["Coupe", "Sedan"],
        f"got {[c.name for c in ordered]}",
    )

    # === Delete on child ===
    print("\n--- Delete ---")
    deleted = await Car.objects.filter(name="Sedan").delete()
    check("Delete Sedan returns 1", deleted == 1, f"got {deleted}")

    car_count = await Car.objects.count()
    check("Car count after delete = 1", car_count == 1, f"got {car_count}")

    # Vehicle count should also decrease
    all_count = await Vehicle.objects.count()
    check("Vehicle count after delete = 3", all_count == 3, f"got {all_count}")

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS sti_vehicles CASCADE")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
