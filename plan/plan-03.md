# plan-03: handoff (tweetAccess)

## สถานะ: สิ่งที่ทำเสร็จและยืนยันแล้ว
- Patch A ทั้งหมด (config/utils/soundpack/storage/service/tabs/feedlist/searchdialog/mainwindow/settings/core/diagnostics) ผ่านการทดสอบ
- Settings: auto_full, feed_pages (20-100 ต่อการเปิดแท็บ), เสียง auto_start/auto_stop
- ชื่อไฟล์ดาวน์โหลดใช้ user_name ของเจ้าของสื่อ (quote ใช้ของทวีตที่ถูก quote) ยืนยันแล้ว
- sort เปลี่ยนแล้ว Apply จัดเรียงทุกแท็บทันที ยืนยันแล้ว
- autoread ตอนปิด main (อ่านจาก cache, โฟกัสแท็บจำตำแหน่ง) ใช้งานได้
- autoread ตอนเปิด main ใช้โหมด "เลียนการกด Space" (`_focus_next_unread` + จังหวะจาก pre_speech/synthDoneSpeaking) ใช้งานได้ ไม่ตีกับ NVDA แล้ว
- สลับบัญชีจาก Settings (main เปิดอยู่) ทำงานเนียนขึ้น หลัง patch `onPageChanging` เช็ก `IsActive()` และ `force_foreground` (log: forced=True active=True)
- probe: operator ค้นหาทั้งหมด, trends, place trends, search_user, followers/following, followers_you_know, muted/blocked, pinned, DM inbox (25 รายการ ฟิลด์ id/is_group/name/participant_ids/partner_id/last_read_event_id/muted/trusted/type/get_history), bookmarks, notifications, lists, posting, media ผ่านแล้ว (ReadTimeout ที่เห็นเป็นเรื่อง X ช้าชั่วคราว)

## ค้างอยู่ (เรียงตามความสำคัญ)

### A. Auto read ตอนเปิด main: auto_full ใช้ไม่ได้
โหมดเลียน Space ให้ NVDA อ่านแถวเอง ซึ่งอ่านได้แค่ข้อความที่ cell เก็บ (ถูกตัดเป็น "(read more...)" เมื่อยาว) จึงไม่ขยายข้อความยาว
ทางเลือก (ยังไม่ตัดสิน):
1. หลัง NVDA อ่านแถวจบ (synthDoneSpeaking) ให้เราพูด "ส่วนที่เหลือ" ของข้อความ (ตัดส่วนที่ cell แสดงแล้วออก) เป็นการต่อท้าย แล้วค่อยรอ auto_pause
2. ก่อน focus แถวที่ยาว ใส่ข้อความเต็มเฉพาะแถวนั้นชั่วคราว (ติดเพดาน ~511 ตัวอักษรของ cell จึงไม่ครบทุกกรณี)
3. ตอนที่ main เปิด ให้พูดเองทั้งหมดเหมือนโหมด cache (กลับไปปัญหาเดิม: cancel/ตีกับ NVDA)
แนะนำทาง 1

### B. Tray icon (สำคัญ ยังไม่แก้)
อาการและหลักฐาน:
- ตอน bgsync ทำงานแล้ว unread เปลี่ยน RemoveIcon+SetIcon ทำให้ 100 ms ต่อมา NVDA focus กระโดดไปปุ่ม tray อื่น (เช่น "Network @ LHC", class `Windows.UI.Input.InputSite.WindowClass`) ทั้งที่ foreground ยังเป็นแอปเดิม (Firefox/Notepad++) log: `TrayFocus AFTER SET 100ms`
- สมมติฐาน (ยังไม่ยืนยัน): Explorer ยิง focus event ของปุ่มที่ถือโฟกัสภายในของ tray หลังปุ่มแรกหายไป ผู้ใช้ลองจัดลำดับไอคอนใหม่ แล้วอาการ "ไม่หาย" (ยังเกิดอยู่ตอนอ่านใน Firefox) จึงไม่ใช่ปัญหาตำแหน่งอย่างเดียว
- ผู้ใช้ย้อน tray.py กลับเวอร์ชันเดิมแล้ว (เวอร์ชันที่ผ่านการปรับจนนิ่ง) และมีเวอร์ชันแยกที่ GPT ใส่ log ไว้ (ตัวหลังนี้ไม่ใช่ตัวหลัก)
- โค้ดหน่วงเวลา/รวมการอัปเดต 1.5 วินาที (core.py `_home_hint`, `_tray_timer`, `_tray_apply`) และ `_focus_in_tray`/`_deferred` ใน tray.py ที่ผมเสนอไป **ผิดทาง** ต้องตรวจว่าถอนออกจากซอร์สจริงแล้ว (patch C1/C2 ในแชทก่อน)
- ผลข้างเคียงใหม่: สลับบัญชีรัว ๆ แล้ว tray ไม่ตามทัน แม้เปิด main ก็ไม่ sync ต้อง restart NVDA และหลังจากนั้นตัวเลขตรงตอนเปิดแต่ไม่ขยับตอนอ่าน (น่าจะเกี่ยวกับ `tray_refresh`/`unread_invalidate` ค้างค่าบัญชีเก่า ต้องตรวจโค้ด)
- ผลข้างเคียงกับ autoread (ตอนปิด main): เมื่อ NVDA ประกาศปุ่ม tray อื่น (เหตุการณ์ข้างบน) autoread เห็นว่ามีเสียงอื่นแทรกแล้วหยุดเอง

แนวทางที่เสนอ (ยังไม่ได้ patch):
1. **ทางหลัก:** ไม่ RemoveIcon เมื่อโฟกัสไม่ได้อยู่ที่ไอคอนเรา ใช้ SetIcon ซ้ำ (NIM_MODIFY) เปลี่ยนแค่ tooltip ตั้งชื่อปุ่มคงที่เป็นชื่อแอป และให้ "N unread" อยู่ใน tooltip ต้องทดสอบว่า NVDA อ่าน tooltip ต่อจากชื่อไหม ส่วนกรณีโฟกัสอยู่ที่ไอคอนเรา ใช้ remove+add+คืนโฟกัสเดิม
2. **สำรอง:** เก็บ focus object ก่อนเปลี่ยนไอคอน ถ้าภายใน ~500 ms NVDA focus ไปตกที่ class tray ทั้งที่ foreground ไม่ใช่ tray ให้ `api.setFocusObject(saved)` + `speech.cancelSpeech()`
3. ให้ autoread ไม่นับการเปลี่ยนโฟกัสไป class tray เป็น "ผู้ใช้แทรก"
ต้องดูซอร์สจริง: tray.py ทั้งไฟล์; core.py: `tray_refresh`, `_tray_counts`, `_apply_tray`, `_on_bg_tick`, `_on_bg_feed`, `unread_invalidate`

### C. Settings เปิดไม่ขึ้นหลังสลับหน้าต่างไปมา (ยังไม่หาย)
อาการ: อ่านโน่นนี่ สลับไป NVSky แล้วกลับมาเปิด Settings เปิดไม่ขึ้น log `bring to front` ออก forced=False (รอบที่เกิดอาการ) รอบที่ปกติ forced=True active=True
สิ่งที่รู้: `shown=True active=False` และ `Raise()` อย่างเดียวไม่พอ (foreground lock) การสลับบัญชีเองทำงานแล้ว
สมมติฐาน (ยังไม่ยืนยัน ต้องตรวจก่อนแก้):
- `open_settings` เช็ก `self._settings is not None and not self._settings.closed` แล้วแค่ `Raise()` ถ้า `_settings` ค้างค่า (dialog ถูกทำลายหรือซ่อนโดยไม่ผ่าน on_close) ก็จะไม่เปิดใหม่เลย
- foreground ตอนนั้นอาจเป็นหน้าต่างของ NVSky (process เดียวกัน, thread เดียวกัน) ทำให้ `AttachThreadInput` ไม่ถูกเรียก (other == mine) และ `SetForegroundWindow` คืน False
- `MainWindow.rebuild()` ตั้ง `_noFocusGrab=False` ก่อนที่ `wx.CallAfter(_focusPanel)` จาก `addTab`/`activate_initial` จะทำงาน
สิ่งที่ผู้ใช้เสนอ: ถ้า flow นี้จะถูกปรับเพราะระบบหลายบัญชีอยู่แล้ว ให้ข้ามไปก่อน แล้วทดสอบใหม่หลังรอบ B
ต้องดูซอร์สจริง: core.py `open_settings`, `settings_closed`, `settings_changed`, `_bring_front`, `_focus_window`; settings.py `SettingsDialog.on_close`; mainwindow.py `rebuild`, `addTab`, `_focusPanel`, `activate_initial`, `onCharHook`
ขอข้อมูลจากผู้ใช้: เปิด Settings ด้วยทางไหน (Ctrl+P ใน main, เมนู NVDA, ปุ่ม tray) และตอนเกิดอาการ หน้าต่าง Settings เก่ายังค้างอยู่เบื้องหลังหรือไม่

### D. Settings > General (ผู้ใช้สั่งแล้ว ยังไม่ทำ)
- จัดหน้า General ใหม่: quick action, speech ("speak when..."), และ interval ต่อแท็บ ผู้ใช้ปรับบ่อย
- ใช้ helper รายชื่อแท็บ (ตัวที่สร้างไว้สำหรับ checklist ของแท็บ) กับ checklist "speak when" ด้วย ปัจจุบันยังไม่ได้นำมาใช้ ต้องขอซอร์ส settings.py ปัจจุบัน

### E. Hashtag (บั๊กจริง)
- Alt+H แสดงรายการ hashtag ได้ แต่กดเลือกแล้วไม่เกิดอะไร ไม่มี error ใน log (น่าจะไม่ได้ผูกกับฟังก์ชันค้นหา)
- หน้าค้นหา: เปลี่ยนที่เติม keyword จาก hashtag จาก combo box เป็น CustomCheckListBox เลือกหลายรายการแล้วเพิ่มทีเดียว (ปุ่ม "Add checked" และโฟกัสรายการแรก)
ต้องดูซอร์สจริง: feedlist.py เมธอดของ Alt+H/เมนู hashtag; searchdialog.py ส่วนเติม hashtag

### F. รอบ B: หลายบัญชี (งานใหญ่ ยังไม่เริ่ม)
ตัดสินใจแล้ว:
- ฐานข้อมูลแยกต่อบัญชี (`accounts/<uid>.db`) ฐานร่วมเก็บแค่รายชื่อบัญชีและ cookie; Config ต่อบัญชี; ไม่ migrate (ช่วงพัฒนา)
- ช่อง "Enable this account in background" ต่อบัญชี
- Alt+W เปิดเมนูรายชื่อบัญชีที่เปิดใช้ (แสดง unread) เพื่อเลือกบัญชีที่แสดงใน main; จำบัญชีล่าสุดแบบเดียวกับ last tab
- tray แสดงบัญชีที่ main แสดงอยู่ (หรือที่ผู้ใช้ล็อก); การแจ้งข้ามบัญชีนำหน้าด้วย @handle
- คิวงานเบื้องหลังรวมศูนย์ แบ่งเท่า ๆ ให้ทุกบัญชี งานที่ผู้ใช้สั่งแทรกก่อน เลี่ยง rate limit
- Settings > Updates: ตารางแท็บทั้งหมดพร้อม interval และคอลัมน์ speak
- การสลับบัญชีต้องผ่านฟังก์ชันกลางตัวเดียว (ดูบทเรียน 3) และโฟกัสต้องไปที่ลิสต์ ไม่ใช่ตัว tab control
- README: คำเตือนความเสี่ยง (ใช้ endpoint ภายใน, หลายบัญชีจาก IP เดียว, rate limit, Shift+F5 สำหรับช่องว่างใหญ่)

### G. รอบ C (หลัง B)
- เมนูผู้ใช้ (Profile, Timeline เป็น submenu, Followers, Following, Known followers, Follow/Mute/Block, Copy URL, Open on web); Alt+U เปิดเมนูนี้
- Pinned tweet: ใช้ `pinned_tweet_ids` (ยืนยันแล้ว) และ `get_user_tweets` คืน pinned เป็นรายการแรกเอง แสดงที่ "บนสุดของทิศทางใหม่สุด" โดยไม่ขยับตาม sort; ตรวจตอนเปิดแท็บ + Ctrl+F5 เท่านั้น
- แท็บ Explore: trends 5 หมวด (Worldwide ได้แค่ 4 รายการ ต้องลอง woeid อื่น), place trends (ตั้งค่าตำแหน่ง), ค้นหาคน, Enter ที่ trend เปิดแท็บค้นหา
- แนบสื่อในหน้าเขียนทวีต: `upload_media` + `create_media_metadata` (alt text) ย่อรูปด้วย Pillow, สูงสุด 4 รูปหรือ 1 วิดีโอ/GIF, self-label เนื้อหาอ่อนไหว (ยืนยันผ่าน probe แล้ว)
- DM: inbox ยืนยันแล้ว (ดูบน) เหลือดู `get_dm_history` กับบทสนทนาจริง (ทดสอบกับคนที่ไม่เคยคุยได้ 0) และข้อความที่เข้ารหัส (คาดว่าเข้าไม่ถึงด้วย cookie ต้องยืนยัน) ให้ลองทำหลังรอบ B
- ค้างประเมิน: `search_tweets_by_date`, `get_user_highlights_tweets`, `get_user_subscriptions`, `get_user_spotlights`, `get_user_mentions`

### H. เรื่องเล็ก
- ความหน่วงเล็กน้อยของ quick action (Like/Bookmark/Retweet): ไม่ใช่บั๊ก (รอ X ตอบ + announce_now หน่วง 100 ms) ถ้าผู้ใช้ต้องการ ทำแบบ optimistic แล้วย้อนกลับเมื่อล้มเหลว (ยังไม่สั่ง เพราะเสี่ยงบอกผิด)
- รอทดสอบ bgsync ของ Notifications ตอนมีของใหม่จริง (log โครงทำงานครบแล้ว) ถ้าไม่เข้า ส่ง 3 บรรทัด `background check notifications` ของรอบนั้น
- probe ปัจจุบันนับจำนวนผลลัพธ์อย่างเดียว ไม่พิสูจน์ว่า X กรองตาม operator จริง (เช่น `from:` คืน 20 เสมอ) ถ้าต้องการ ให้ probe ตรวจผู้เขียน/สื่อจริง
- probe ตอน post test: เพิ่มลองลบซ้ำ 3 ครั้งแล้ว (diagnostics.py `_delete_with_retry`)

## รายการ TEMP ที่ต้องลบ (เมื่อยืนยันแล้ว)
- core.py: `log.info(... "background check %s: started/nothing/ok=.../%d new" ...)`
- core.py `_bring_front`: `log.info(TAG + "bring to front: forced=... active=...")  # TEMP`
- tray.py (ตัวทดลองของ GPT, ถ้ายังมี): `TrayFocus BEFORE/AFTER ...`, `_log_focus_later`

## ลำดับที่แนะนำสำหรับแชทหน้า
1. ตรวจซอร์สจริงว่าโค้ดหน่วง tray ถูกถอนครบ (core.py + tray.py)
2. แก้ tray (B) ด้วยทางหลัก แล้วทดสอบ 3 กรณี: โฟกัสแอปอื่น, โฟกัสที่ไอคอนเรา, สลับบัญชีรัว ๆ
3. แก้ hashtag (E) และหน้า General (D)
4. ตัดสินทาง auto_full (A) แล้ว patch
5. รอบ B (หลายบัญชี) ซึ่งจะแทนที่ flow ของข้อ C
6. รอบ C

## ที่ต้องอัปโหลดในแชทหน้า
- ซอร์สทั้งหมดของ tweetAccess (ฉบับปัจจุบัน) + plan-03.md + project instruction ที่ปรับแล้ว
- ไม่ต้องแนบ NVSky ทั้งโปรเจกต์ แนบเฉพาะ autoread.py ฝั่ง NVSky ถ้าจะ sync โค้ดสองฝั่ง
- log ล่าสุดของ tray และ bring to front ที่มี