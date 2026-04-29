"""Interactive guidebook retrieval tester — type a query, see what comes back."""
from guidebook import retrieve_guidebook

print("=== Guidebook Retrieval Tester ===")
print("Type a query (or 'q' to quit)\n")

while True:
    query = input(">> ").strip()
    if not query or query.lower() == "q":
        break
    result = retrieve_guidebook(query)
    if result:
        print(f"\n{result}\n")
    else:
        print("\n(no match)\n")
