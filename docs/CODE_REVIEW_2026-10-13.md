# مراجعة أكواد JALWE وAPEX — 2026-10-13

راجعت بنية ملفات Python في المشروعين، نقاط تشغيل الخدمات، حصر جهة تنفيذ الأوامر، ومسارات الدخول والوقف والتخفيف والاستعادة والتقارير والتقييم البحثي. استخدمت تحليل الصياغة لكل الملفات وفحص الأسماء غير المعرفة F821/F823، ثم اختبارات الوحدات الحالية واختبارات حالات جديدة. هذا لا يثبت خلو جميع المسارات من الأخطاء، ولا يثبت ربحية الاستراتيجيات. الاختبارات الجديدة تستخدم وسيطًا ومزود بيانات مزيفين ولا ترسل صفقات أو رسائل خارجية.

## أخطاء أُعيد إنتاجها وصُححت

| المسار | قبل | بعد |
|---|---|---|
| أسعار إدارة الصفقة | قبول Infinity/NaN قد يلوث أعلى سعر أو ينشئ قرار هدف | رفض القيم غير المحدودة قبل تعديل حالة الصفقة؛ استبعادها من قراءة الأسعار |
| كمية الأسهم | 3.9 قد تصبح 3 دون اعتراض عند بناء الصفقة | قبول عدد صحيح موجب فقط |
| استجابة الوقف | REJECTED/CANCELED/FILLED تظهر كحماية فعالة | حماية فعالة للحالات غير النهائية المسموحة فقط، مع حفظ هوية الأمر للاستعادة |
| RSI | الصعود المتواصل بلا خسائر يعطي بيانات مفقودة | RSI=100 بعد الفترة المطلوبة؛ بيانات الإحماء والنسبة المسطحة غير المعرفة تظل غير متاحة |
| صلاحية سعر الدخول | فحص الوسيط قد يستغرق وقتًا يجعل السعر منتهيًا قبل الإرسال | إعادة فحص القرار والسعر داخل callback قبل حفظ SUBMITTING وإرسال POST |
| إيقاف الدخول | ملف تحكم تالف أو قيمة نصية false قد يسمحان بالشراء | فشل القراءة أو نوع غير صالح يمنع دخولًا جديدًا؛ لا يختلق طلب بيع طارئ |
| توقيت إيقاف الدخول | طلب الإيقاف أثناء الفحص لا يعاد قراءته مباشرة قبل الشراء | إعادة قراءة إيقاف الدخول عند الإرسال؛ البيع والاستعادة يبقيان مستقلين |
| موائم أسعار APEX القديم | فشل المزود يولد شموعًا عشوائية | بيانات غير متاحة دون توليد أسعار |
| نموذج ML القديم | درجة عشوائية موصوفة كاحتمال نجاح | رفض واضح عند غياب نموذج صالح |
| خيارات APEX القديمة | IV/Delta/سيولة/درجة ثابتة توصف كعقد مُقيّم | UNAVAILABLE دون قيم مختلقة |
| مرجع السوق القديم | غياب SPY يولد سلسلة عشوائية لتصنيف السوق | UNKNOWN عند غياب المرجع |
| أهداف الأداة القديمة | غياب الشموع ينتج وقفًا وأهدافًا افتراضية | UNAVAILABLE دون مستويات |

الأدوات الخمس القديمة المعدلة في APEX ليست واردة في سلسلة استيراد نقطة التشغيل الحالية apex_research_loop.py. لذلك وجودها لا يثبت أنها سببت صفقات الخسارة السابقة. أزيل سلوكها المضلل حتى لا تستخدم لاحقًا كأدلة سوق. مستويات target_risk_engine القديمة تعتمد متوسط high-low وليست ATR، وهذا موضح في الوصف دون تغيير قواعد الاستراتيجية الفعلية.

## وظيفة ملفات JALWE

يبدأ التشغيل عبر railway_controller.py الذي يشغّل watcher وحلقة البحث كعمليات تابعة. وجود ملفات AI في الجذر وفي intelligence لا يعني سلطة تداول مزدوجة: الجذر يخدم الأبحاث، وintelligence يخدم قرار JALWE الداخلي.

| الملف | وظيفته |
|---|---|
| [ai_engine.py](../ai_engine.py) | يمنح البحث درجة واتجاهًا بقواعد محددة؛ الدرجة ليست احتمال نجاح مثبتًا. |
| [apex_research_loop.py](../apex_research_loop.py) | حلقة بحث للأسهم ونشر نتائج الفحص؛ لا تنفذ أوامر. |
| [broker/__init__.py](../broker/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [broker/alpaca_client.py](../broker/alpaca_client.py) | العميل المركزي لحساب Alpaca PAPER، الأوامر والمراكز والتقويم؛ تنفيذ الوسيط يتم هنا. |
| [broker/entry_pricing.py](../broker/entry_pricing.py) | يحسب سقف سعر أمر شراء Limit مع تقريب محافظ يناسب الحد المسموح. |
| [broker/paper_data_client.py](../broker/paper_data_client.py) | موائم بيانات وفهرس أصول Alpaca للفحص؛ عميل حسابه PAPER. |
| [core/__init__.py](../core/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [core/config.py](../core/config.py) | إعدادات التشغيل والأمان ورأس المال والحدود؛ التداول LIVE مقفول. |
| [core/database.py](../core/database.py) | يحفظ أوامر الدخول وحالة الصفقات وسجل PnL؛ يسجل التنفيذ وحالته معًا في معاملة SQLite. |
| [core/lifecycle_audit.py](../core/lifecycle_audit.py) | يحفظ نقاط متابعة دورة الصفقة وحالة الوقف. |
| [core/manual_sell_ui.py](../core/manual_sell_ui.py) | يعرض ويؤكد اختيارات البيع اليدوي في واجهة التحكم. |
| [core/manual_sells.py](../core/manual_sells.py) | طابور طلبات البيع اليدوية على الصفقات التي يديرها JALWE. |
| [core/models.py](../core/models.py) | تعريف موحد للميزات والإشارات والمخاطرة والأوامر وحالاتها. |
| [core/persistence_probe.py](../core/persistence_probe.py) | فحص استمرارية حفظ البيانات عبر إعادة التشغيل. |
| [core/runtime_controls.py](../core/runtime_controls.py) | يحفظ إيقاف/استئناف الدخول وطلب الإغلاق الطارئ؛ فشل قراءة التحكم يمنع الدخول. |
| [core/storage.py](../core/storage.py) | يحدد مجلد البيانات الدائم ومسارات قواعد البيانات. |
| [core/target_audit.py](../core/target_audit.py) | يجهز تدقيق لمس الأهداف والتنفيذ للصفقة؛ يفصل الملاحظة عن البيع المحقق. |
| [decision_outcome_memory.py](../decision_outcome_memory.py) | يحفظ أدلة القرار الأصلي وأسباب الرفض ومتابعة حركة السعر بشكل مستقل. |
| [delayed_sip_research.py](../delayed_sip_research.py) | يعيد تقييم النوافذ التاريخية ببيانات SIP متأخرة؛ لا يستخدمها لتنفيذ البيع أو الشراء. |
| [intelligence/__init__.py](../intelligence/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [intelligence/ai_engine.py](../intelligence/ai_engine.py) | تقييم قواعد JALWE الداخلي؛ يختلف عن درجة أبحاث APEX. |
| [intelligence/breakout_confirmation_engine.py](../intelligence/breakout_confirmation_engine.py) | يتحقق من إغلاق شمعة الاختراق والحجم والمسافة فوق المستوى. |
| [intelligence/decision_engine.py](../intelligence/decision_engine.py) | يجمع البوابات الداخلية ويعيد فحص الجودة عند تأكيد الاختراق؛ مصدر قرار الشراء النهائي. |
| [intelligence/external_research_bridge.py](../intelligence/external_research_bridge.py) | يتحقق من أبحاث APEX وحداثتها ويحفظ تاريخها؛ لا يمنح تصريح تنفيذ. |
| [intelligence/feature_engine.py](../intelligence/feature_engine.py) | يحسب RVOL وVWAP وATR وRSI والاتجاه والزخم وجودة البيانات. |
| [intelligence/learning_engine.py](../intelligence/learning_engine.py) | يستخرج أدلة من صفقات PAPER مغلقة مؤهلة ويربطها بميزات قبل الدخول. |
| [intelligence/news_engine.py](../intelligence/news_engine.py) | سياق الأخبار الداخلي مع إظهار غياب المصدر عند تعذره. |
| [intelligence/opportunity_engine.py](../intelligence/opportunity_engine.py) | يقيّم جودة الفرصة ويقرر اجتياز حدها. |
| [intelligence/options_flow_engine.py](../intelligence/options_flow_engine.py) | يجلب بيانات عقود الخيارات كسياق؛ ليس أمر تداول خيارات. |
| [intelligence/session_strategy_engine.py](../intelligence/session_strategy_engine.py) | يقارن ست استراتيجيات الجلسة: قمة ما قبل الافتتاح، نطاق الافتتاح، Gap & Go، العلم، VWAP، والانضغاط. |
| [intelligence/strategy_router.py](../intelligence/strategy_router.py) | يوجّه أنواع الفرص وحدود المخاطرة بحسب السوق والدرجات. |
| [intelligence/trigger_engine.py](../intelligence/trigger_engine.py) | يفحص الوصول لمستوى التفعيل والمسافة منه وشروط إلغاء الفكرة. |
| [jalwe_bridge_client.py](../jalwe_bridge_client.py) | ينشر ويقرأ أبحاث APEX عبر PostgreSQL أو SQLite حسب الإعداد. |
| [jalwe_research_watcher.py](../jalwe_research_watcher.py) | يتابع أبحاث APEX، يطلب قرار JALWE، ويدير الصفقات والوقف والخروج والتحقق من التنفيذ. |
| [liquidity_engine.py](../liquidity_engine.py) | يقيس نشاط السعر والحجم والسيولة من بيانات متاحة. |
| [market/__init__.py](../market/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [market/bar_quality.py](../market/bar_quality.py) | يستبعد الشموع الداخلية التي لم تغلق دون ملء الشموع المفقودة. |
| [market/gap_evidence.py](../market/gap_evidence.py) | يفحص عينة تاريخية محدودة لصفقات دقيقة غائبة؛ لا يصنع سعرًا أو يكمل نافذة ناقصة. |
| [market/market_context.py](../market/market_context.py) | يجلب بيانات مؤشرات السوق لبناء سياق القرار. |
| [market/market_data.py](../market/market_data.py) | قراءة الشموع والصفقات والأسعار؛ يرفض السعر غير المحدود ويفحص صلاحية الوصول إلى SIP. |
| [market/market_discovery.py](../market/market_discovery.py) | يكتشف أصولًا حقيقية ويمنع الاعتماد على قائمة سوق مصطنعة. |
| [market/market_regime.py](../market/market_regime.py) | يصنف حالة السوق من بيانات المؤشرات؛ ليس استراتيجية تنفيذ مستقلة. |
| [market/research_bar_quality.py](../market/research_bar_quality.py) | يتحقق من صلاحية شموع التقييم البحثي ومن قيم OHLCV. |
| [market/research_diagnostics.py](../market/research_diagnostics.py) | يفصل البيانات المفقودة عن المرفوضة ويعرض تشخيص تغطية المصدر. |
| [market/session_features.py](../market/session_features.py) | يحسب قمة ما قبل الافتتاح ونطاق الافتتاح وأحجام جلسة نيويورك. |
| [news_engine.py](../news_engine.py) | يجلب الأخبار ويقيّم حداثتها ومحفزاتها للبحث. |
| [opportunity_performance_tracker.py](../opportunity_performance_tracker.py) | يتابع نوافذ فرص 60 دقيقة ويقيس الأهداف/الوقف وMFE/MAE؛ لا يحسب أرباح تداول فعلية. |
| [pre_breakout_engine.py](../pre_breakout_engine.py) | يقيّم شروط ما قبل الاختراق عبر عدة أطر زمنية؛ ليس تصريح شراء. |
| [railway_controller.py](../railway_controller.py) | يشغّل العمليات التابعة، يستقبل تحكم Telegram، ويعرض رصيد الاستراتيجية والتقارير وتنبيهات أوامر الوسيط. |
| [research_orchestrator.py](../research_orchestrator.py) | يجمع نتائج الفحص والسيولة والأخبار وتحليل ما قبل الاختراق في تقرير بحث. |
| [research_recheck.py](../research_recheck.py) | يحدد موعد إعادة فحص التقرير دون تجاوز بوابات القرار. |
| [research_variants.py](../research_variants.py) | محاكاة إعادة الاختبار ووقف الجزء المتبقي ومقارنة الوقف بمخاطرة ثابتة؛ نتائج بحثية. |
| [scanner_engine.py](../scanner_engine.py) | يجلب قائمة الأسهم ويرتب المرشحين وفق بيانات السوق. |
| [self_update/__init__.py](../self_update/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [self_update/cli.py](../self_update/cli.py) | أوامر حالة ومراجعة التحديث المحدود للكود. |
| [self_update/contract_worker.py](../self_update/contract_worker.py) | اختبارات رقمية مستقلة لدوال التحويل المحددة. |
| [self_update/contracts.py](../self_update/contracts.py) | يشغل فحص الحسابات في عملية مستقلة لا ترث بيانات الاعتماد. |
| [self_update/policy.py](../self_update/policy.py) | يفحص حدود تعديلات AST المسموح بها للتحديث المحدود. |
| [self_update/publisher.py](../self_update/publisher.py) | ينشر التعديل الذي اجتاز شروط الاختبار والنطاق إلى GitHub. |
| [service_health.py](../service_health.py) | يسجل حالة الخدمة ودورات العمل دون تنفيذ تداول. |
| [strategy_experiments.py](../strategy_experiments.py) | يجمع تجارب الاستراتيجيات المتقابلة ويختبر شروط اختيار تفضيل PAPER. |
| [strategy_policy_guard.py](../strategy_policy_guard.py) | يحفظ إصدارات التفضيلات ويراقب أداءها البحثي اللاحق وشروط إيقافها. |
| [trading/__init__.py](../trading/__init__.py) | تهيئة حزمة Python؛ ليس استراتيجية أو مسار تداول مستقلًا. |
| [trading/entry_quality.py](../trading/entry_quality.py) | يتحقق من النشاط الحالي وعمر السعر والمصدر والفارق نسبةً لمسافة الوقف. |
| [trading/execution_engine.py](../trading/execution_engine.py) | يفحص القرار والمراكز ويعيد فحص الإيقاف والسعر قبل POST؛ المقبول ليس منفذًا. |
| [trading/exit_sessions.py](../trading/exit_sessions.py) | يحدد أهلية جلسة البيع؛ الخروج الممتد يحتاج سعرًا حيًا صالحًا وأمر Limit مناسبًا. |
| [trading/exit_submission.py](../trading/exit_submission.py) | يحفظ هوية البيع قبل الإرسال ويستعيد الإرسال غير المحسوم عبر client_order_id. |
| [trading/paper_trade_orchestrator.py](../trading/paper_trade_orchestrator.py) | ينظم حفظ نية الدخول وإرسالها وانتظار التنفيذ وإدارة الصفقة وتفعيل الوقف. |
| [trading/protective_fills.py](../trading/protective_fills.py) | يطبق فروق التنفيذ التراكمي للوقف مرة واحدة ويحسب PnL بالمتوسط التراكمي. |
| [trading/protective_submission.py](../trading/protective_submission.py) | يميز رفض الوقف المثبت عن إرسال غير محسوم ويحدد هل استجابة الأمر فعالة. |
| [trading/reconciliation_engine.py](../trading/reconciliation_engine.py) | يقرأ حالة الأمر والمركز الفعليين من الوسيط ويؤكد الكمية والسعر. |
| [trading/recovery_engine.py](../trading/recovery_engine.py) | يستعيد الطلبات المعلقة بعد الانقطاع ويمنع التكرار والمراكز مجهولة المصدر. |
| [trading/risk_engine.py](../trading/risk_engine.py) | يحسب كمية الأسهم والمخاطرة من رصيد الاستراتيجية والكاش المتاح وحدود الخسارة. |
| [trading/trade_manager.py](../trading/trade_manager.py) | حالة الصفقة والتخفيف: T1 يرفع الوقف للدخول، T2 إلى T1، T3 إلى T2 ثم وقف الجزء المتبقي. |

## وظيفة ملفات APEX

المسار الحالي يمر عبر حلقة البحث والماسح والفحص الكامل ومحركات البحث وذاكرة الأفكار والجسر وفحص الخدمة. ملفات التنفيذ القديمة مغلقة. بقية أدوات V3 ليست جهة دخول أو خروج في JALWE.

| الملف | وظيفته |
|---|---|
| [active_trade_manager.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/active_trade_manager.py) | توافق قديم للقراءة فقط؛ لا يدير مراكز الوسيط أو يبيعها. |
| [ai_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/ai_engine.py) | يمنح البحث درجة واتجاهًا بقواعد محددة؛ الدرجة ليست احتمال نجاح مثبتًا. |
| [apex_research_loop.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/apex_research_loop.py) | حلقة بحث للأسهم ونشر نتائج الفحص؛ لا تنفذ أوامر. |
| [backtest_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/backtest_engine.py) | اختبار تاريخي مبسط ببيانات Yahoo؛ لا يعيد تشغيل صفقات JALWE الفعلية أو تكاليفها. |
| [bot.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/bot.py) | فحص اتصال Telegram يدوي عند تشغيل الملف؛ ليس حلقة التداول. |
| [bridge_client.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/bridge_client.py) | اسم توافق للعميل الذي ينشر البحث إلى جسر JALWE. |
| [chart_and_learning_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/chart_and_learning_engine.py) | أداة قديمة لميزات الرسم والتعلم؛ خارج حلقة APEX الحالية. |
| [config.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/config.py) | إعدادات V3 القديمة؛ ليست حدود مخاطرة JALWE V4. |
| [dashboard.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/dashboard.py) | لوحة قديمة لعرض PAPER؛ ليست نقطة تشغيل الخدمة الحالية. |
| [database.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/database.py) | قاعدة بيانات V3 القديمة للتقارير المحلية. |
| [database_manager.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/database_manager.py) | مخزن قديم للصفقات والميزات والنماذج والاستراتيجيات. |
| [event_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/event_engine.py) | سياق أحداث بحثي؛ يعرض UNKNOWN عند غياب مزود موثوق. |
| [execution_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/execution_engine.py) | مسار تنفيذ قديم معطّل؛ يرفض إرسال أوامر APEX. |
| [full_market_scan.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/full_market_scan.py) | فحص دفعات أصول السوق بقراءة بيانات Alpaca دون أوامر. |
| [jalwe_bridge_client.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/jalwe_bridge_client.py) | ينشر ويقرأ أبحاث APEX عبر PostgreSQL أو SQLite حسب الإعداد. |
| [learning_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/learning_engine.py) | ملاحظات تعلم V3 قديمة من نتائج يوفرها المستدعي؛ ليست تعلم JALWE الداخلي. |
| [liquidity_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/liquidity_engine.py) | يقيس نشاط السعر والحجم والسيولة من بيانات متاحة. |
| [main.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/main.py) | البرنامج الأحادي القديم متقاعد؛ يوقف التشغيل برسالة واضحة. |
| [market_data_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/market_data_engine.py) | موائم قديم لبيانات الشموع؛ يعيد إطارًا فارغًا عند تعذر البيانات ولا يختلق أسعارًا. |
| [market_regime.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/market_regime.py) | تصنيف قديم باستخدام تغير SPY وVIX المقدمين له. |
| [market_scanner.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/market_scanner.py) | ماسح أسهم قديم وواجهة توافق؛ المصدر الحالي scanner_engine/full_market_scan. |
| [ml_predictor.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/ml_predictor.py) | واجهة احتمال ML قديمة متقاعدة؛ ترفض إنتاج احتمال دون نموذج مثبت. |
| [news_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/news_engine.py) | يجلب الأخبار ويقيّم حداثتها ومحفزاتها للبحث. |
| [options_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/options_engine.py) | تقييم قديم لبيانات عقد يقدمها المستدعي؛ ليس تداول خيارات ولا مصدر صلاحية JALWE. |
| [options_flow_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/options_flow_engine.py) | واجهة خيارات قديمة؛ تعيد UNAVAILABLE دون اختلاق IV أو سيولة أو درجة. |
| [pre_breakout_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/pre_breakout_engine.py) | يقيّم شروط ما قبل الاختراق عبر عدة أطر زمنية؛ ليس تصريح شراء. |
| [regime_detector.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/regime_detector.py) | تصنيف قديم من بيانات مرجعية يقدمها المستدعي؛ غيابها ينتج UNKNOWN. |
| [research_orchestrator.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/research_orchestrator.py) | يجمع نتائج الفحص والسيولة والأخبار وتحليل ما قبل الاختراق في تقرير بحث. |
| [risk_manager.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/risk_manager.py) | حدود مخاطرة V3 قديمة؛ لا تعتمد عليها سلطة التنفيذ الحالية. |
| [run_bot.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/run_bot.py) | مشغل قديم متقاعد؛ يوجّه إلى apex_research_loop.py. |
| [scan_audit.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/scan_audit.py) | سجل مختصر لخطوات المسح وجودة البيانات وأسباب الاستبعاد. |
| [scanner_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/scanner_engine.py) | يجلب قائمة الأسهم ويرتب المرشحين وفق بيانات السوق. |
| [scheduler.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/scheduler.py) | جدولة V3 القديمة؛ خارج حلقة التشغيل الحالية. |
| [service_health.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/service_health.py) | يسجل حالة الخدمة ودورات العمل دون تنفيذ تداول. |
| [setup_memory.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/setup_memory.py) | يحفظ أفكار التداول البحثية ويعيد فحصها بسعر وبيانات حديثة. |
| [strategy_lab.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/strategy_lab.py) | محاكاة استراتيجية قديمة؛ نتائجها ليست PnL المحفظة ولا اختبارًا كافيًا للربحية. |
| [target_risk_engine.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/target_risk_engine.py) | مستويات بحثية قديمة من متوسط النطاق اليومي، لا ATR؛ غياب البيانات ينتج UNAVAILABLE. |
| [technical_indicators.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/technical_indicators.py) | مؤشرات SMA وRSI قديمة؛ خارج مسار الفحص الحالي. |
| [telegram_bot.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/telegram_bot.py) | واجهة توافق لإرسال إشعارات APEX وفق إعدادات الهدوء. |
| [telegram_notifier.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/telegram_notifier.py) | إرسال إشعارات قديم؛ ليس تنفيذ تداول. |
| [trade_monitor.py](https://github.com/18badr19-sudo/jalwe-ai-trader/blob/main/trade_monitor.py) | مراقب توافق متقاعد لا ينفذ بيعًا أو شراءً. |

## التحقق وحدود النتائج

- قبل التعديل: JALWE 340 اختبارًا، وAPEX 46. بعد الإصلاح: JALWE 349 وAPEX 52. محليًا نجح 395 اختبارًا وتجاوزت البيئة 6 اختبارات PostgreSQL لعدم إعداد TEST_POSTGRES_DSN؛ تُفحص تكاملات PostgreSQL في GitHub Actions.
- استُخدمت اختبارات فشل قبل الإصلاح للتحقق من أن حالات الخلل قابلة لإعادة الإنتاج؛ تكرر تشغيلها مع المجموعتين بعد الإصلاح.
- جميع ملفات Python اجتازت تحليل الصياغة وفحص F821/F823. هذا فحص تركيب وأسماء، وليس إثبات سلامة كل منطق الملف.
- قواعد الوقف المتفق عليها محفوظة: T1 إلى الدخول، T2 إلى T1، T3 إلى T2 ثم إدارة الجزء المتبقي. السهم الواحد يتقدم بالحماية دون بيع أجزاء غير ممكنة.
- APEX يظل بحثًا، وJALWE وحده يرسل أوامر Alpaca PAPER، وLIVE مقفول. لم تُضف العملات الرقمية ضمن هذه المراجعة.
- نقص شموع IEX/SIP ليس صفراً في حركة السعر. النوافذ غير المكتملة لا تتحول إلى نجاح أو فشل مثبت. لا يجوز مقارنة MFE بربح المحفظة.
- رفض الوصول إلى بيانات SIP الحية متعلق باشتراك المزود؛ الكود لا يستطيع إصلاح صلاحية الحساب. البيع الممتد الذي يتطلب تلك البيانات يظل مؤجلًا إذا تعذر الحصول عليها.
- أرباح وخسائر سجل الاستراتيجية تستند إلى التنفيذ المؤكد؛ فرق رصيد حساب Alpaca الكامل يحتاج تدقيق الأنشطة قبل نسبته إلى رسوم أو خطأ.
- الملفات القديمة والمحاكاة المبسطة ليست تقييمًا معتمدًا للربحية، ولا تتحكم تلقائيًا بمخاطرة الصفقات.
