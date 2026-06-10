from chat.services.expense_extraction import extract_expense_items, parse_from_to_locations

msg = "আজকে ধানমন্ডি থেকে বনানী গিয়েছি ১৮০ টাকা।"
pair = parse_from_to_locations(msg)
ext = extract_expense_items(msg)
lines = [f"route={pair}"]
for it in ext.items:
    lines.append(
        f"cat={it.category!r} amt={it.amount} "
        f"frm={it.from_location!r} to={it.to_location!r}"
    )
lines.append(f"malformed={ext.malformed}")
open("dbg_exp.txt", "w", encoding="utf-8").write("\n".join(lines))
