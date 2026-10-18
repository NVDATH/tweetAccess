# plan-04: handoff (tweetAccess)

## สถานะ: ยืนยันแล้วว่าใช้งานได้ (ผู้ใช้ทดสอบ)

- **Tray (ล็อกที่แนวทางปัจจุบัน):**
  - ข้อความ `tweetAccess: N unread` ถูกต้อง ไม่อ่านซ้ำ ใช้ RemoveIcon + SetIcon
  - ไม่แตะ tray ระหว่างมีหน้าต่างของเราเปิด (main หรือ settings) อัปเดตครั้งเดียว 400 ms หลังหน้าต่างสุดท้ายปิด (`_windows_open`, `_tray_dirty`, `_flush_tray` ใน core.py)
  - `TrayIcon.swallow_stray_focus` + `GlobalPlugin.event_gainFocus` กลบ "Network" ที่ Explorer ยิงมาหลัง remove (ยืนยันจาก log: เกิด ~100 ms หลัง remove, role=9, foreground ไม่ใช่ `Shell_TrayWnd`)
  - ไม่ส่งค่า unread = 0 ชั่วคราวเข้า tray ตอนล้างลิสต์ (`_update_status(notify=False)` ใน `_reset_list`/`__init__`)
- **บั๊ก `_target` ถูกทับ:** `ThreadPanel`/`QuotesPanel` ใช้ `_tweet_id` แล้ว ยืนยันแล้ว
- **Hashtag (E):** `HashtagDialog` (CustomCheckListBox, ไม่ติ๊กอะไรใช้รายการที่โฟกัส) และ "Add checked" ในหน้าค้นหา ยืนยันแล้ว
- **Settings > General:** จัดลำดับใหม่ (Enter action, Quote actions, Speak when, interval 4 ช่อง, Auto read, Load how many, Download folder) ผู้ใช้สั่งทำ ยังไม่เห็นผลทดสอบยืนยันชัด ให้ถือว่ารอยืนยัน
- **Settings เปิดไม่ขึ้น:** มีทางกันตาย (`force_foreground` ลอง F24 แล้ว `open_settings` แทนหน้าต่างเก่าถ้าดึงไม่ขึ้น) log ยืนยันว่าทำงาน (replacing it) สาเหตุจริงยังไม่รู้
- ลบ log TEMP ทั้งหมดแล้ว (core.py, tray.py, mainwindow.py)
- autoread ทั้งสองโหมดทำงานได้
- settings > general ที่บอกว่ายังไม่ยืนยันด้านล่าง ยืนยันแล้วทั้งหมด 

## ตัดสินใจแล้ว: Known issues (ใส่ README)

- **auto_full ไม่มีผลกับ auto read ตอนเปิด main** ผู้ใช้สั่งปิดประเด็นนี้
  - ลองแล้วสองแบบ: (1) พูดส่วนที่เหลือต่อท้าย ใช้ไม่ได้เพราะ NVDA พูดลำดับคอลัมน์, created at, ตำแหน่ง ("1 of 20") ก่อน (2) ขยายข้อความ cell เฉพาะแถว (แผน ข.) ผู้ใช้ไม่เห็นว่าได้ผล ถอนกลับแล้ว
  - ข้อจำกัดคือ ListCtrl cell ~500 ตัวอักษร (ผู้ใช้พบจาก NVSky) จึงมี `(read more...)` และห้ามยัดข้อความเต็มลง cell
  - ข้อความ README: *Auto read with the main window open reads each row as NVDA does, so "Read the whole message of long tweets" only applies when the main window is closed.*
  - ถ้าจะแก้ป้ายช่อง auto_full ให้ตรงความจริง จะกระทบการแปล (ยังไม่ได้ตัดสิน)
- ผู้ใช้คืน autoread.py กับ feedlist.py/utils.py กลับเป็นสภาพก่อนแผน ข. เองจากไฟล์สำรอง ซอร์สที่ต้องใช้ในแชทหน้าคือฉบับหลังคืน

## รอ / ค้างติดตาม (ยังไม่ยืนยันสาเหตุ)

1. **อีเมลนักพัฒนา NVDA add-on** (ร่างเสร็จแล้ว ผู้ใช้จะส่งเอง) ถ้ามีคำตอบเรื่องเปลี่ยน name ปุ่ม tray ในที่เดิม หรือดัก event ก่อน `doPreGainFocus` (เช่น `decide_executeEvent`, overlay) ให้กลับมาแก้ tray
   - อาการที่เหลือ: เปิด main จากไอคอน tray แล้วปิด ตอนอยู่แอปอื่น title ของหน้าต่างที่ focus กลับมาถูกตัดคำพูด สาเหตุจาก trace: `doPreGainFocus` เรียก `cancelSpeech` 4 ms ก่อนถึง `event_gainFocus` ของเรา (เกินการควบคุมระดับ plugin) ผู้ใช้ยอมรับ
   - **ยังไม่ยืนยัน:** เสียง Network ตอน bgsync อัปเดตไอคอนในแอปอื่น (ผู้ใช้ได้ยินครั้งหนึ่ง ทดสอบตามสั่งไม่ได้) ถ้าเกิดอีกโดยไม่มีบรรทัด `swallowed` ให้เพิ่ม log gainFocus ชั่วคราวดูว่าหลุดเงื่อนไขไหน (เวลาเกิน 1.5 s หรือ role/foreground)
2. **Settings เปิดไม่ขึ้นหลังสลับบัญชี > ปิด main:** log แสดง `had=True live=True` แต่ `forced=False active=False` (หน้าต่างยังอยู่ แต่ Windows ไม่ยอมดึงเป็น foreground) ผู้ใช้ชี้ว่าตอน Set active หน้า main เด้งขึ้นเอง และ settings ค้างอยู่ด้านหลัง ยังไม่รู้ว่าอะไรเด้ง main
3. **Notifications โหลดซ้ำครั้งแรกหลัง patch บางรอบ:** เดาว่า cache รุ่นเก่ามี id สูตรต่างกัน (`stamp*1000 + crc32 % 1000`) ถ้าเกิดซ้ำโดยไม่ได้แก้โค้ด ให้ดูว่าแถวซ้ำมีเวลาเท่ากันไหม (ถ้าเท่ากัน แปลว่า id ไม่นิ่ง)
4. **Thread เปิดแล้วเงียบ (progress sound แล้วนิ่ง ไม่มี error):** log รอบทดสอบแสดง callback ทำงานปกติ (`ok=True posts=24`) ยังไม่ซ้ำได้ ถ้าเกิดอีก ต้องดู log ตอนนั้น (log TEMP ถอนแล้ว ต้องเพิ่มกลับ)
5. **สลับบัญชีรัว ๆ ตัวเลข tray:** ผู้ใช้บอกทำงานได้ แต่ยัง "รอเฟิร์ม"

## ค้างตามแผนเดิม (ตามลำดับ)

1. ยืนยัน Settings > General ที่จัดลำดับใหม่ (ลำดับ Tab, mnemonic ไม่ซ้ำ, ค่าค้างหลัง Apply)
2. **รอบ B: หลายบัญชี (งานใหญ่ ยังไม่เริ่ม)** ตัดสินใจแล้ว:
   - ฐานข้อมูลแยกต่อบัญชี (`accounts/<uid>.db`) ฐานร่วมเก็บรายชื่อบัญชีกับ cookie; Config ต่อบัญชี; ไม่ migrate
   - ช่อง "Enable this account in background" ต่อบัญชี
   - Alt+W เมนูบัญชีที่เปิดใช้ (แสดง unread) จำบัญชีล่าสุดแบบ last tab
   - tray แสดงบัญชีที่ main แสดง (หรือที่ล็อก); การแจ้งข้ามบัญชีนำหน้า @handle
   - คิวงานเบื้องหลังรวมศูนย์ แบ่งเท่า ๆ ทุกบัญชี งานผู้ใช้แทรกก่อน
   - Settings > Updates: ตารางแท็บทั้งหมดพร้อม interval และคอลัมน์ speak (ตอนนี้ interval/speak เป็นตามหมวด `_bg_announces` ตัดสินตามหมวด การทำตามชื่อแท็บต้องแก้ core.py ด้วย จึงเลื่อนมารอบ B)
   - สลับบัญชีผ่านฟังก์ชันกลางตัวเดียว โฟกัสไปที่ลิสต์ ไม่ใช่ tab control ซึ่งจะแทนที่ flow ข้อ 2 ของค้างติดตาม
   - README: เตือนความเสี่ยง (endpoint ภายใน, หลายบัญชีจาก IP เดียว, rate limit, Shift+F5 สำหรับช่องว่างใหญ่)
3. **รอบ C:** เมนูผู้ใช้ (Alt+U), pinned tweet, แท็บ Explore (trends, place trends, ค้นหาคน), แนบสื่อในหน้าเขียนทวีต (`upload_media` + `create_media_metadata`, ย่อรูปด้วย Pillow, สูงสุด 4 รูปหรือ 1 วิดีโอ/GIF), DM (inbox ยืนยันแล้ว เหลือ `get_dm_history` กับบทสนทนาจริงและข้อความที่เข้ารหัส), ประเมิน `search_tweets_by_date`, `get_user_highlights_tweets`, `get_user_subscriptions`, `get_user_spotlights`, `get_user_mentions`
4. เรื่องเล็ก: quick action หน่วงเล็กน้อย (ไม่ใช่บั๊ก ถ้าต้องการทำ optimistic แล้วย้อน), รอทดสอบ bgsync ของ Notifications ตอนมีของใหม่จริง (ถ้าไม่เข้า ส่ง 3 บรรทัด log `background check notifications` ซึ่งตอนนี้ถอนแล้ว ต้องเพิ่มกลับ), probe ยังไม่พิสูจน์ว่า X กรองตาม operator จริง

## ข้อสังเกตที่ควรจำ

- **เมธอดของคลาสแม่ vs attribute:** ห้ามตั้ง attribute ชื่อเดียวกับเมธอดของ `TweetPanel` (กรณี `_target` ทำให้ `'str' object is not callable`) ตรวจชื่อก่อนเพิ่ม attribute ในคลาสลูก
- **tray:** เปลี่ยน tooltip ที่เดิมไม่ได้ NVDA อ่านต่อท้ายชื่อเสมอ (name ถูกตรึงตอน NIM_ADD) จึงต้อง remove + add และ remove ทำให้ Explorer ยิง focus ไปปุ่มอื่น ห้ามแก้ด้วยการหน่วง/รวมอัปเดต และการตัด `api.setFocusObject` ไม่ช่วยเรื่องคำพูดถูกตัด
- ผู้ใช้โฟกัสได้ทีละหน้าต่าง การไม่อัปเดต tray ตอนหน้าต่างเราเปิดอยู่จึงไม่กระทบผู้ใช้
- ผู้ใช้สำรองไฟล์ก่อนทดลอง patch เสี่ยง และถอนเองได้ ไม่ต้องส่ง patch ถอนให้ ยกเว้นขอ
- ผู้ใช้ไม่ต้องการให้คาดหวังว่ารอบไหนจะผ่าน: แยก "ยืนยันแล้ว" กับ "เดา" ให้ชัดเสมอ

## รายการ TEMP

ไม่เหลือ (ถอนหมดแล้ว) `_saved_focus` ใน tray.py ไม่มีอะไรใช้แล้ว (ตัด `setFocusObject` ไปแล้ว) ปล่อยไว้ได้ เก็บกวาดตอนรอบใหญ่

## ที่ต้องอัปโหลดในแชทหน้า

- ซอร์สทั้งหมดของ tweetAccess ฉบับปัจจุบัน (หลังคืน autoread.py, feedlist.py, utils.py จากไฟล์สำรอง) + plan-04.md + project instruction
- log ล่าสุดถ้ามีอาการ Settings ไม่ขึ้น หรือ Network แทรกตอน bgsync
- คำตอบอีเมลจากนักพัฒนา NVDA (ถ้ามี)
- NVSky: แนบเฉพาะ autoread.py ฝั่ง NVSky ถ้าจะ sync และรายการที่ต้องส่งต่อ NVSky เมื่อ tray ผ่านจริง: remove + SetIcon, ไม่แตะ tray ตอนหน้าต่างเปิด, ไม่ส่ง 0 ชั่วคราว, ตรวจ attribute ทับชื่อเมธอดคลาสแม่, กลบ stray focus ใน `event_gainFocus` (ผู้ใช้สั่งพักการส่งต่อจนกว่าจะยืนยัน)

## ลำดับที่แนะนำสำหรับแชทหน้า

1. ยืนยัน General, อัปเดต README known issues
2. ตามอาฟการค้างติดตาม ถ้ายังเกิด (ขอ log)
3. รอบ B (หลายบัญชี) ซึ่งเปลี่ยน flow สลับบัญชี/Settings
4. ส่งต่อ NVSky เมื่อผู้ใช้อนุญาต
5. รอบ C