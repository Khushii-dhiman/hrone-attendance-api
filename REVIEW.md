# Code Review: Starter Code Flaws & Fixes

### 1. Off-by-one Pagination Bug (`/employees`)
- **Flaw**: Starter code mein `skip = page * page_size` use ho raha tha. Page 1 request karne par pehle 20 records skip ho jaate the.
- **Fix**: Isse badal kar `skip = (page - 1) * page_size` kiya gaya.

### 2. Incorrect Total Count on Filtered Queries
- **Flaw**: Department query param pass hone ke bawajood `total = db.employees.count_documents({})` hardcoded tha jo poore database ka count deta tha.
- **Fix**: Count query mein filter dictionary pass kiya: `db.employees.count_documents(q)`.

### 3. In-Memory Sorting & Memory Leak Risk (`/attendance`)
- **Flaw**: Saare documents memory mein load karke Python list sort (`sort(key=...)`) aur slice kiya ja raha tha, jo scalable nahi hai.
- **Fix**: Direct database-level pagination aur indexing use ki: `db.attendance_logs.find(q).sort("date", -1).skip(skip).limit(page_size)`.

### 4. Unhandled 404 & Silent Exceptions (`/punch-in`)
- **Flaw**: Invalid employee code par employee lookup `None` return karta tha, aur code `emp["shift_start"]` par crash hoke `500 Internal Server Error` fekta tha.
- **Fix**: Explicit `404 HTTPException` add kiya gaya jab employee record exist na kare.

### 5. Overnight Shift Calculation Issue
- **Flaw**: Overnight shift (jaise 22:00 se 06:00) ke case mein shift end time date string ke hisaab se pichle date ka man liya jata tha jisse overtime negative ya zero ho jata tha.
- **Fix**: `compute_overtime` mein logic add kiya gaya jo `end_ist` ko next day par advance karta hai agar shift midnight cross karti hai.