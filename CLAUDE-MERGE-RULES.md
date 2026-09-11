# Claude Unicorn merge rules (for Grok follow-up if still needed)

Target: ONE product per (name, dose); unit=vial; price=LOWER per-vial; kit_price=HIGHER kit.
Prefer upsert / active=0 over hard delete. Never wipe /data/inventory.db.
Rename Anav@r → Anavar 25mg.
Strip $price name suffixes from fix_unicorn_names fallback.
Do NOT auto-merge B12 $10/$15, SluPP variants, Snap 8 $8/$50, MT1/MT2 $11/$30, Oxytocin $10/$30 — flag those.
Keep all writes on MagicFactory2Bot bound chat_id.
