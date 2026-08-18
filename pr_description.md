⚡ Optimize membership check using set instead of list

💡 **What:** Replaced list `["NYSE", "NASDAQ"]` with a set `{"NYSE", "NASDAQ"}` for membership checks in `tools/fetch_stock_info.py`.

🎯 **Why:** Sets use a hash table and provide O(1) average time complexity for membership testing (`in` operator), compared to O(n) for lists. This is a clear suboptimal data structure choice that can be trivially fixed.

📊 **Measured Improvement:**
I measured the performance of iterating 10,000,000 times on a membership check.

For the "Found" case (where exchange is "NYSE"), the set membership check is slightly slower because the item is at the start of the list so list checking is very fast (0.3789 seconds vs 0.4264 seconds).

However, for the "Not Found" case (where exchange is "BSE", which is the case that takes longer to search in a list), the set membership check is significantly faster:
- List membership check: 0.9042 seconds
- Set membership check: 0.4695 seconds
- Improvement: **48.08%** faster

This small optimization creates more consistent latency regardless of the exchange checked and follows Python performance best practices.
