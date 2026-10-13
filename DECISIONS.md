# Architectural & Design Decisions

### 1. Database Schema & Indexing Strategy
- **Unique Compound Index (`emp_code` + `date`)**: Attendance logs table par unique compound index lagaya gaya hai taaki koi bhi employee ek hi din mein do baar punch-in na kar sake aur application-level race conditions avoid hon.
- **Single Field Index (`emp_code`)**: Employees collection mein unique index create kiya gaya hai fast lookups aur duplicate prevention ke liye.
- **Index (`date`, `status`)**: Daily department summaries aur status filters ki query performance optimize karne ke liye banaya gaya hai.

### 2. Timezone & Datetime Handling
- Shift timings IST (UTC+05:30) mein define hain, jabki database mein BSON UTC format store hota hai.
- API requests aur responses epoch milliseconds (`int`) deal karte hain jaisa openapi contract specify karta hai, aur internal calculations IST awareness ke saath execute hoti hain.
- Overnight shift logic handle kiya gaya hai jahan `shift_end <= shift_start` ho, wahan shift agle din complete maani jaati hai taaki overtime negative calculate na ho.

### 3. Data Integrity & Audit Trails
- Manual attendance correction (`PATCH /attendance/{emp_code}/{date}`) seedha field overwrite nahi karta balki har modification ke saath `history` array mein actor (`by`), reason (`reason`), timestamp (`at`), aur exact field-level diff (`changes: {field: {from, to}}`) store karta hai.
- Punch time badalne par derived attributes (`late_minutes`, `work_hours`, `overtime_minutes`, `half_day`) automatically recompute hote hain.

### 4. Aggregation Pipelines & Edge Cases
- Department summary pipeline employee headcount aur logs ko `$lookup` ke through join karta hai taaki jin employees ka us din koi log nahi hai, wo bhi correct headcount mein reflect hon.
- Leaderboard sorting secondary tie-breaker `emp_code` ke saath configure ki gayi hai deterministic results ke liye.