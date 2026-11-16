# البنية المعمارية

```
main.py                     نقطة التشغيل
sarraf_recon/
  money.py                  تحليل المبالغ (Decimal) والتخزين بالوحدة الصغرى
  textnorm.py               توحيد النص العربي، المراجع، التشابه
  db.py                     مخطط SQLite، الترحيل (PRAGMA user_version)، المعاملات، الإعدادات
  importers/
    readers.py              Excel (xlsx/xls) / CSV / PDF  ->  RawTable (قيم خام)
    base.py                 تعرّف الأعمدة، Mapping، parse_table -> ParsedRow (txn/opening/closing/skip)
  engine.py                 محرك المطابقة (دوال نقية بدون قاعدة بيانات): propose / allocate / score
  services.py               عمليات العمل داخل معاملات + سجل العمليات (audit_log)
  reconciliation.py         الأرصدة والفروقات والتصنيفات لكل حساب/عملة/فترة
  report.py                 تقرير Excel
  ui/                       واجهة PySide6 (RTL)
tests/                      اختبارات pytest (محاسبة، مطابقة، استيراد، واجهة)
tools/make_demo.py          بيانات تجريبية وهمية
START.bat / build_exe.bat / SarrafRecon.spec
```

## تدفق البيانات

```
ملف ─► readers ─► RawTable ─► detect_mapping (اقتراح) ─► [مراجعة المستخدم] ─► parse_table ─► ParseResult
   ─► services.import_statement:
        نسخ الملف إلى data/originals/<sha256>
        لكل حركة: fingerprint + occurrence ─► جديدة أو "ظهرت مرة أخرى" (batch_sightings)
        علامة "بأثر رجعي" + "تكرار مشتبه"
        الأرصدة الافتتاحية/الختامية ─► statement_balances
   ─► services.run_matching ─► engine.propose ─► matches (proposed | approved تلقائيًا)
   ─► المستخدم: approve / reject / unmatch / manual_match / mark_duplicate
   ─► reconciliation.compute ─► اللوحة والتقرير
```

## الجداول الرئيسية

| الجدول | الغرض |
|---|---|
| `accounts` | الصرافون. `mirror=1`: كشف الصراف من منظوره |
| `import_batches` | كل استيراد: الملف، البصمة، المسار المحفوظ، الإحصاءات، التحديد المستخدم، التنبيهات |
| `transactions` | الحركات. `UNIQUE(account, side, fingerprint, occurrence)` يمنع الاحتساب المزدوج |
| `batch_sightings` | كل ظهور لحركة في أي ملف |
| `statement_balances` | أرصدة واردة في الكشوفات (افتتاحي/ختامي) أو يدوية، باصطلاح الكشف نفسه |
| `matches` / `match_items` | المطابقات وتخصيص المبالغ. الحالات: proposed, approved, rejected, unmatched |
| `rejected_signatures` | المقترحات المرفوضة حتى لا تُقترح مرة أخرى |
| `audit_log` | سجل كامل لكل عملية |
| `import_profiles` | قوالب تحديد الأعمدة لكل صراف |

لا يُحذف أي سجل مطابقة معتمد. فك المطابقة يغيّر حالتها إلى `unmatched` مع السبب والمستخدم والوقت.

## الضمانات

- **عدم تجاوز المبلغ:** `services._check_available` قبل كل اعتماد، ثم `trg_match_approve_guard` في SQLite.
  ولا يمكن إدخال مطابقة بحالة `approved` مباشرة (`trg_match_insert_guard`).
- **المعاملات:** كل عملية كتابة داخل `BEGIN IMMEDIATE ... COMMIT`، وتُلغى بالكامل عند أي خطأ.
- **الدقة:** `Decimal` للتحليل والعرض، وأعداد صحيحة للتخزين والجمع.
- **الرصيد الأساسي:** أقدم رصيد افتتاحي معروف لكل جانب وعملة هو نقطة البداية. الأرصدة اللاحقة في الكشوفات تُقارن
  بالمحسوب من الحركات (صفحة "تحقق أرصدة الكشوفات" في التقرير)، فتكشف الصفوف الناقصة بدل أن تخفيها.
- **المطابقة في تاريخ معين:** عند حساب فترة تنتهي في تاريخ ما، لا تُحتسب إلا المطابقات التي كل حركاتها في هذا
  التاريخ أو قبله.

## نقاط التوسعة

- قارئ مخصص لصراف معيّن: دالة تُرجع `RawTable` (أو `ParseResult` مباشرة) في `importers/`، تُبنى بعد
  استلام عينات حقيقية.
- قواعد تسجيل إضافية: `engine.score_pair` و `engine.group_score`.
