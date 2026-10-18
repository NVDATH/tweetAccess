# plan-05: handoff (tweetAccess)

ต่อจาก plan-04 (ใช้แทน plan-04 ได้เลย) ทุกอย่างในนี้แยก "ยืนยันแล้ว" กับ "เดา/ยังไม่ยืนยัน" ไว้ชัดเจน

## สถานะ: ยืนยันแล้ว (ผู้ใช้ทดสอบ)

- **B1 ฐานข้อมูลแยกต่อบัญชี:** `Store` เป็นตัวหน้า (facade) ฐานร่วม `tweetaccess.db` เก็บรายชื่อบัญชี cookie บัญชีที่ active ธง `bg_enabled:<id>` และแถว cfg กลางรุ่นเก่า ส่วนฐานต่อบัญชี `accounts\<id>.db` เก็บ tweets และ meta ต่อบัญชี (tab state, cache, ตำแหน่งอ่านค้าง, ค่า `cfg:*`) ใช้คีย์ไฟล์เดียวกันทั้งหมด (`tweetaccess.key`) ชื่อไฟล์เป็นเลข id ภายในตาราง accounts (ไม่ใช่ uid ของ X) ตั้งใจให้ระบุตัวตนไม่ได้ ย้ายแถวเก่าเข้าไฟล์ของบัญชีหนึ่งครั้งตอนเปิดครั้งแรก (ธง `migrated_from_shared`) ลบบัญชี = ลบไฟล์ของบัญชี
- **B2a ฟังก์ชันสลับบัญชีกลาง:** `switch_account(account_id, focus=False, after_menu=False)` ใน core.py เป็นทางเดียวที่สลับบัญชี `accounts_changed(focus)` เป็นจุดเดียวที่ rebuild หน้าต่างหลัก (ใช้ทั้งเพิ่ม ลบ สลับ) ทุกครั้งที่เรียกจะหยุด auto read โหลดค่าของบัญชีใหม่ และรีโหลด sound pack
- **B2b เมนูบัญชี:** Alt+W ในหน้าต่างหลัก (เฉพาะหน้าต่างหลัก ไม่มี global) เมนู tray มีรายชื่อบัญชีเมื่อมีมากกว่าหนึ่ง (`account_choices`, `choose_account`) เสียงประกาศหลังเมนูปิดใช้ `announce_after_menu` ใน utils.py (รอจน NVDA ย้ายโฟกัสออกจากเมนูก่อนพูด) ส่วน Alt+W ใช้ `_settle_switch` (โฟกัสลงลิสต์ แล้วประกาศ)
- **B2c ค่าต่อบัญชี:** `Config(store, account_id)` มี `load(account_id)` ค่าทุกอย่างของหน้า General, Display, Sound แยกต่อบัญชีอย่างเด็ดขาด หน้า Accounts เป็นส่วนเดียวที่ใช้ร่วมกัน ครั้งแรกที่บัญชีหนึ่งถูกโหลด คัดลอกค่ากลางเดิมเข้าไปหนึ่งครั้ง (ธง `cfg_imported`) ช่อง "Enable this account in background" อยู่หน้า Accounts ใช้ทันทีที่ติ๊ก เก็บใน meta ฐานร่วม หน้าตั้งค่าที่เป็นต่อบัญชีมี `PER_ACCOUNT = True` กด Set as active ใน Settings จะบันทึกค่าที่พิมพ์ค้างให้บัญชีเดิมก่อน แล้วสร้างสามหน้านั้นใหม่จากค่าของบัญชีใหม่ ชื่อหน้าต่าง Settings บอกบัญชี
  - ทดสอบแล้วผ่าน: ค่า Settings แยกต่อบัญชี, สลับใน Settings แล้วโฟกัสไม่หลุด, ช่อง background บันทึกค่า
- **หน้าต่างหลักไม่เด้งทับ Settings:** สาเหตุยืนยันจาก stack: `notebook.AddPage` ใน wx ทำให้หน้าต่างที่ไม่ active ถูก activate เอง แก้โดย `MainWindow.rebuild` สั่ง `Disable()` ระหว่าง rebuild เมื่อไม่ได้ขอโฟกัสและหน้าต่างไม่ active แล้ว `Enable()` ใน `finally`
- **thread / quotes โหลดเพิ่ม:** สาเหตุยืนยันจากซอร์ส twifork 2.4.0: `get_tweet_by_id` ให้ reply หน้าแรกหน้าเดียว และ `search_tweet` ให้หน้าแรกหน้าเดียว แก้ให้เปิดแท็บและ F5 อ่านตาม `feed_pages` และ Shift+F5 โหลดหน้าถัดไป (ประมาณ 20 ต่อครั้ง จำตำแหน่งใน `FetchService._chains` คีย์ `thread:<id>` และ `quotes:<id>`) ผู้ใช้ยืนยัน: เปิดครั้งแรกได้ 60 ตามที่ตั้ง, Shift+F5 ได้ครั้งละ 20, sync เบื้องหลังของ thread + quote ที่เปิดทิ้งไว้ทำงานและมีเสียงแจ้ง
- **Ctrl+Shift+T คืนแท็บที่ปิด:** เก็บในหน่วยความจำของปลั๊กอิน ต่อบัญชี สูงสุด 20 แท็บ (`remember_closed_tab`, `pop_closed_tab`) คืนที่ตำแหน่งเดิม พร้อมทวีต สถานะอ่านแล้ว และแถวที่โฟกัส (ผู้ใช้ยืนยันหลังแก้ลำดับการเขียนค่าตำแหน่งก่อนสร้างแท็บ) ไม่มีแท็บให้คืนจะแจ้ง
- **NVDA+Shift+F5 (update Home):** ทำงานเหมือน F5 ของ Home เป็นคำสั่ง global ที่ผู้ใช้ขอเพื่อทดสอบ ผู้ใช้ทดสอบบน Firefox ดูปกติ (ถอนค่าท่าทางได้จาก Input gestures ถ้าไม่ต้องการ)
- **tray: แก้ที่ต้นเหตุแล้ว (ผู้ใช้ประเมินว่าได้ผล)**
  - ต้นเหตุ: ผู้ที่ถือโฟกัส tray คือ Explorer ไม่ใช่ NVDA เมื่อเราลบปุ่มของเรา Explorer ย้ายโฟกัสภายในไปปุ่มข้างเคียง (เช่น Network) แล้ว UIA แจ้ง NVDA เป็นเหตุการณ์โฟกัสจริง NVDA ประมวลใน `doPreGainFocus` ก่อนถึง plugin: เปลี่ยน focus object ไปที่ปุ่ม tray, เรียก `event_foreground` ซึ่งสั่ง `speech.cancelSpeech()` autoread ฟังสัญญาณ cancel และหยุด ตัวกลบเสียงเดิมใน `event_gainFocus` ช่วยไม่ได้
  - วิธีแก้: overlay `StrayTrayFocusGuard` (tray.py) ที่ปุ่มบนแถบงานทิ้งเหตุการณ์โฟกัสผ่าน `shouldAllowUIAFocusEvent = False` ก่อนเข้าคิวของ NVDA เมื่อรากหน้าต่างของปุ่มเป็นแถบงาน (`Shell_TrayWnd`, `TopLevelWindowForOverflowXamlIsland`, `NotifyIconOverflowWindow`) และหน้าต่างหน้าสุดของ Windows ไม่ใช่แถบงาน (กฎโครงสร้าง ไม่มีช่วงเวลา) เลือก overlay ผ่าน `GlobalPlugin.chooseNVDAObjectOverlayClasses` → `choose_overlay` กลไกเดียวกับที่ explorer appModule ของ NVDA ใช้กับหน้าต่างสลับงาน (ตรวจกับซอร์ส NVDA สาขา master)
  - หลักฐาน: log รอบทดสอบทิ้งเหตุการณ์แปลกปลอม 15 ครั้ง ไม่มีเหตุการณ์หลุด ตอนเข้าถาดจริง (Win+B) ไม่ถูกทิ้งและอ่านปกติ และไม่มี cancelSpeech ที่มาจากเหตุการณ์แปลกปลอม
  - ถอดตรรกะกันเดิมทั้งชุดแล้ว (patch ลบ 22 จุด) ไอคอนอัปเดตทันทีทุกเมื่อ ไม่มี `_tray_blocked`, `_tray_dirty`, `_flush_tray`, `window_deactivated`, `swallow_stray_focus`, `event_gainFocus`, `_settings_forward` เหลือ `_restore_focus` ไว้สำหรับกรณีอัปเดตเข้ามาตอนผู้ใช้กำลังอยู่ที่ไอคอนเรา (อาจกระพริบหนึ่งครั้ง ตั้งใจ)
  - ที่ผู้ใช้ยืนยันชัด: main unread change แล้ว Win+B อ่านไว ๆ ไม่กระพริบแล้ว ส่วนอีก 6 ข้อของชุดทดสอบสุดท้ายผู้ใช้ประเมินคร่าว ๆ ว่าไม่มีปัญหา (ไม่ได้ยืนยันทีละข้อ)
  - log ถาวรเดียว: `log.debug("dropped a focus event that Explorer raised by itself")` ขึ้นเฉพาะตอนเปิด debug logging
- **TEMP:** ไม่เหลือ (ลบหมดแล้วรวม TEMP ของ quotes และ announce)
- **ข้อจำกัดที่ยืนยันแล้ว:** add-on รองรับ NVDA 2026.1 ขึ้นไปเท่านั้น (ไม่รองรับ Windows 10 ซึ่งไม่เป็นปัญหา)

## ตัดสินใจแล้ว: Known issues (ใส่ README)

- ข้อความเดิมของ auto_full จาก plan-04 ยังใช้ตามเดิม: *Auto read with the main window open reads each row as NVDA does, so "Read the whole message of long tweets" only applies when the main window is closed.*
- จำนวนใน thread/quotes ไม่ตรงกับตัวเลขใน tweet info เสมอ: X ซ่อน reply/quote บางส่วน, reply count นับเฉพาะ reply ตรง ๆ ส่วน thread รวมทวีตต้นทางและ reply ซ้อน (ส่วนสาเหตุของ "ไม่ตรง" เป็นการเดาจากพฤติกรรม ไม่ได้ยืนยันจากฝั่ง X)
- F5 และ sync เบื้องหลังในแท็บ thread/quotes อ่านใหม่ตาม `feed_pages` ถ้าโหลดเพิ่มด้วย Shift+F5 ไปไกลกว่านั้น รายการจะย่อกลับ
- แท็บที่ปิดไปแล้ว คืนได้ด้วย Ctrl+Shift+T เฉพาะในรอบ NVDA ที่เปิดอยู่ (ปิด NVDA แล้วหาย)
- เลือกหลายแถวแบบไม่ติดกันทำไม่ได้ (Ctrl+Space และ Space ถูกใช้ไปแล้ว) ผู้ใช้ตัดสินใจว่าไม่จำเป็น ปิดเรื่องนี้ (Ctrl+A และ Shift+ลูกศรเลือกหลายแถวได้อยู่แล้ว ใช้กับ Mark as read/unread ในเมนู Alt+A)
- ช่อง Enable this account in background มีผลเฉพาะบัญชีที่ active จนกว่า B3 จะเสร็จ
- README ต้องเตือนความเสี่ยงตามเดิม: endpoint ภายใน, หลายบัญชีจาก IP เดียว, rate limit, Shift+F5 สำหรับช่องว่างใหญ่

## รอ / ค้างติดตาม (ยังไม่ยืนยันสาเหตุ)

1. **Notifications tab โหลดซ้ำเป็นบางวัน** ผู้ใช้เห็นอีกครั้ง คาดว่าไม่เกี่ยวกับ patch: เดาว่าเกิดเมื่อวันเปลี่ยนและมีการแจ้งเตือนใหม่ที่เพิ่มมาเมื่อวัน (ผู้ใช้มีการแจ้งเตือนไม่บ่อย จึงสรุปไม่ได้) ถ้าเกิดซ้ำ ให้ดูว่าแถวซ้ำมีเวลาเท่ากันไหม (ถ้าเท่ากัน id ไม่นิ่ง: สูตร `stamp*1000 + crc32 % 1000` ใน `notification_to_post`) ผู้ใช้สั่งให้ใส่เป็นคิวงานไว้ทำภายหลัง
2. **เสียง Network ตอน bgsync ขณะอยู่แอปอื่น:** เชื่อว่าแก้แล้วด้วย overlay ถ้าเกิดอีก ให้เปิด debug logging ของ NVDA ดูบรรทัด "dropped a focus event" และถามผู้ใช้ว่าก่อนหน้านั้นทำอะไร (ถ้าโฟกัสของ NVDA ค้างที่ปุ่ม tray ทั้งที่แอปอื่นอยู่หน้าสุด แปลว่ามีเหตุการณ์ที่กฎไม่ครอบคลุม)
3. **Thread เปิดแล้วเงียบ (progress sound แล้วนิ่ง):** ไม่พบอีกในรอบนี้ ถ้าเกิดต้องเพิ่ม log กลับ
4. **quick action หน่วงเล็กน้อย** (ไม่ใช่บั๊ก ถ้าต้องการทำ optimistic แล้วย้อน) และ **bgsync ของ Notifications ตอนมีของใหม่จริง** ยังไม่ได้ยืนยัน (ถ้าไม่เข้า ต้องเพิ่ม log กลับ)
5. probe ยังไม่พิสูจน์ว่า X กรองตาม operator จริง: ส่วนของ `quoted_tweet_id:` ยืนยันแล้วว่าใช้ได้ (ผลทั้ง 60 เป็น quote จริงในทวีตที่ทดสอบ) ส่วน operator อื่นยังไม่ยืนยัน

## ค้างตามแผน (ตามลำดับ)

1. **voice profile for autoread (งานแรกของแชทหน้า)** ผู้ใช้อยากให้ฟีเจอร์ autoread สมบูรณ์ตามที่วางไว้
   - ผมถามไปแล้วว่าหมายถึงแบบไหน (ผู้ใช้ยังไม่ได้ตอบ ต้องถามใหม่ตอนเริ่มแชท): (ก) ให้ autoread สลับไปใช้ voice profile ของ NVDA ที่ผู้ใช้เลือกไว้ระหว่างอ่าน แล้วคืนค่าเมื่อหยุด หรือ (ข) โปรไฟล์เสียงที่ผูกกับตัว add-on เอง
   - **ยังไม่ได้ค้นข้อมูลใด ๆ** (ผู้ใช้สั่งให้รอ) ต้องค้นก่อนออกแบบ: API ของ NVDA สำหรับเปิด/ปิดโปรไฟล์ด้วยมือและตอน autoread ทำงานสองโหมด (เปิด main: ตามเสียงของแถวที่ NVDA อ่านเอง / main ปิด: เราพูดเองจากแคช) และผลต่อ `pre_speech`/`pre_speechCanceled` ที่ autoread ใช้ตรวจการถูกขัด
   - autoread.py ใช้ร่วมกับ NVSky: ถ้าเปลี่ยน ต้องบอกผู้ใช้เพื่อ sync
2. **ทดสอบค้างของ B2c:** เพิ่มและลบบัญชีใน Settings (หน้าอื่นสร้างใหม่ถูก ไม่มี error), ช่อง Enable in background แบบใช้เวลา (ตั้ง Home 5 นาที ปิดช่อง รอเกิน 6 นาที ต้องไม่โหลดเบื้องหลังเลย ส่วนบัญชีที่เปิดช่องต้องตรวจปกติ), sound และรูปแบบ tray แยกตามบัญชี (ผู้ใช้บอกว่า sound pack ยังทำ default ไม่เสร็จ)
3. **รอบ B ที่เหลือ (B3):**
   - คิวงานเบื้องหลังรวมศูนย์ แบ่งเท่า ๆ ทุกบัญชีที่เปิดช่อง Enable งานผู้ใช้แทรกก่อน
   - tray แสดงบัญชีที่ main แสดง (หรือที่ล็อก) การแจ้งข้ามบัญชีนำหน้า @handle ตัวเลข unread ในเมนูบัญชีตอนนี้นับเฉพาะ Home ต้องรวมแท็บตามที่ตั้งไว้
   - Settings > Updates: ตารางแท็บทั้งหมดพร้อม interval และคอลัมน์ speak (ตอนนี้ตามหมวด `_bg_announces`)
   - README เตือนความเสี่ยง (ดูด้านบน)
4. **รอบ C** (ยังไม่เริ่ม): เมนูผู้ใช้ (Alt+U), pinned tweet, แท็บ Explore (trends, place trends, ค้นหาคน), แนบสื่อในหน้าเขียนทวีต (`upload_media` + `create_media_metadata`, ย่อรูปด้วย Pillow, สูงสุด 4 รูปหรือ 1 วิดีโอ/GIF), DM (inbox ยืนยันแล้ว เหลือ `get_dm_history` กับบทสนทนาจริงและข้อความที่เข้ารหัส), ประเมิน `search_tweets_by_date`, `get_user_highlights_tweets`, `get_user_subscriptions`, `get_user_spotlights`, `get_user_mentions`
5. **คิวที่ผู้ใช้ฝากไว้ (ยังไม่ค้นข้อมูล ต้องถามรายละเอียดก่อนทำ):**
   - move custom checkbox: ยังไม่รู้ว่าหมายถึงย้ายช่องติ๊กตัวไหนไปที่ไหน หรือทำช่องติ๊กใหม่แทน `CustomCheckListBox` (ถามไปแล้ว ผู้ใช้ยังไม่ได้ตอบ)
   - keymaps settings: ยังไม่รู้ว่าหมายถึงหน้าตั้งค่าคีย์ลัดในหน้าต่างหลัก หรือคีย์ลัด global ผ่าน Input gestures (ถามไปแล้ว ยังไม่ได้ตอบ)
6. **ส่งต่อ NVSky (ผู้ใช้สั่ง: รอรวบตอนรอบ B เสร็จ ผมเป็นคนเขียนสรุปและส่งซอร์สให้):** ฝั่ง NVSky ผู้ใช้มีบัญชี Bluesky บัญชีเดียว จึงทดสอบการสลับบัญชีจริงไม่ได้ ต้องส่งเป็นสรุปภาษาอังกฤษพร้อมซอร์สตัวอย่างโดยไม่ต้องทดสอบจริง รายการ: เหตุผลที่ `AddPage` ของหน้าต่างที่ไม่ active ทำให้ wx activate เอง (ปิดด้วย Disable/Enable), ฟังก์ชันสลับบัญชีจุดเดียวที่หยุด autoread, ค่าต่อบัญชี, เมนูบัญชีที่ประกาศหลังเมนูปิดด้วย `announce_after_menu`, และกลไก tray ทั้งชุด (overlay `shouldAllowUIAFocusEvent` แทนการกลบเสียง, เหตุผลจากโค้ด NVDA: `doPreGainFocus` → `event_foreground` → `cancelSpeech`, ไม่มี `decide_executeEvent`) รายการจากแผนเดิมที่ยังต้องส่ง: remove + SetIcon, ตรวจ attribute ทับชื่อเมธอดคลาสแม่ นอกจากนี้ผู้ใช้บอกว่าฝั่ง NVSky ถ้าเปิด main แล้วย้ายไปทวีตที่ยังไม่อ่านจะยังมีบั๊กเดิม รอแก้รอบเดียวกัน
   - **autoread:** ผู้ใช้จะขอภายหลังเมื่อต้องการ sync และขอให้ผมสรุป autoread ฝั่งเราให้ก่อน (แยกโหมดเปิด main กับโหมดแคช, ข้อจำกัด `auto_full`, ตัวฟัง `pre_speech`/`pre_speechCanceled`) ผู้ใช้สั่ง NVSky ให้ทำฟีเจอร์ autoread ใหญ่เพิ่มแล้ว แต่ฝั่งนั้นทำช้า ยังไม่เคย patch autoread จากฝั่งนั้น
7. **เก็บกวาดรอบใหญ่:** `tabs.make_temp_tab` ซ้ำโครงกับ `build_tabs` (ควรรวม), แถวเก่าค้างในฐานร่วม (ตาราง tweets และ meta ต่อบัญชีรุ่นก่อน B1, ค่า cfg กลางรุ่นก่อน B2c) ลบด้วยโค้ดแยก + VACUUM (ผู้ใช้ยังไม่ได้สั่ง ควรรอใช้งานจริงสักระยะ), `_saved_focus` และ `STRAY_WINDOW` ถูกลบหมดแล้ว

## ข้อสังเกตที่ควรจำ

- **เมธอดของคลาสแม่ vs attribute:** ห้ามตั้ง attribute ชื่อเดียวกับเมธอดของ `TweetPanel` (กรณี `_target`) ตรวจชื่อก่อนเพิ่ม (ใช้ grep ทั้งโปรเจกต์) ชื่อที่เพิ่มรอบนี้และตรวจแล้วไม่ชน: `snapshot`, `_load_more`, `_more_text`, `_job_options`, `on_more_result`
- **wx:** `AddPage` ของหน้าต่างที่ไม่ active ทำให้ activate หน้าต่างนั้น ต้องกันด้วย `Disable()` ระหว่างสร้าง ส่วนเมนู popup ปิดแล้ว Windows ยังคืนโฟกัสตามหลัง: ใช้ `announce_after_menu` รอจนโฟกัสของ NVDA ออกจากเมนูแล้วค่อยพูด
- **NVDA:** ลำดับเหตุการณ์โฟกัส: UIA → `shouldAllowUIAFocusEvent` → `queueEvent` → `executeEvent` → `doPreGainFocus` (เปลี่ยน focus object, อาจเรียก `foreground` ซึ่งสั่ง `cancelSpeech`) → `event_gainFocus` ของ plugin ดังนั้นการกลบใน plugin ช้าเกินไป ต้องทิ้งที่ `shouldAllowUIAFocusEvent` ผ่าน overlay วัตถุที่ Explorer ถืออยู่ภายในคือโฟกัสของ Explorer ไม่ใช่ของ NVDA
- **twikit/twifork:** `get_tweet_by_id(tweet_id, cursor=None)` คืน reply หน้าแรก (`tweet.replies` เป็น `Result` ที่มี `next()`) ส่วนหน้าถัดไปจาก `next()` เป็น reply ระดับบนไม่มีตัวซ้อน `search_tweet(count<=20)` คืนทีละหน้า
- **ผู้ใช้ไม่ต้องการ patch สำหรับการแก้ตัวเลขแถวเดียว** (เช่นค่าคงที่): บอกว่าแก้ที่แถวไหนพอ ผู้ใช้ไม่ต้องการให้ไล่อ่านของเก่า: ทุกรอบที่ถามว่าเหลืออะไรให้ทดสอบ ให้สรุปรายการค้างทั้งหมดและสิ่งที่ต้องรายงานให้ครบในข้อความเดียว
- ผู้ใช้สำรองไฟล์ก่อนทดลอง patch เสี่ยง และถอนเองได้ ไม่ต้องส่ง patch ถอนให้ ยกเว้นขอ ผู้ใช้ไม่ต้องการให้คาดหวังว่ารอบไหนจะผ่าน: แยก "ยืนยันแล้ว" กับ "เดา" ให้ชัดเสมอ
- **วิธีตรวจ patch ก่อนส่ง (ใช้ได้ผลดีรอบนี้):** ให้ผมประกอบซอร์สสภาพล่าสุดจากไฟล์ที่อัปโหลด แล้ว apply patch ด้วยสคริปต์ที่ตรวจว่า old_str เจอหนึ่งที่ต่อจุด คอมไพล์ และทดสอบตรรกะที่ไม่ผูก wx ด้วยตัวจำลอง (เช่น client ปลอมของ twikit) แต่ส่วน wx/NVDA ทดสอบไม่ได้
- สตริงใหม่ตั้งแต่ B2b ถึงตอนนี้ต้องรวมเข้าไฟล์แปลก่อนปล่อย (มี `# Translators:` ครบแล้ว)

## ที่ต้องอัปโหลดในแชทหน้า

- ซอร์สทั้งหมดของ tweetAccess ฉบับปัจจุบัน (หลังลง patch ทุกชุดของแชทนี้ รวม patch ลบ 22 จุด) + plan-05.md + project instruction
- ซอร์ส autoread.py ฝั่ง NVSky และผล diagnostics จาก NVSky ถ้าจะ sync เรื่อง autoread
- log ล่าสุดถ้ามีอาการ (Notifications โหลดซ้ำ, เสียง Network หลุด)

## ลำดับที่แนะนำสำหรับแชทหน้า

1. voice profile for autoread: ถามความหมายให้ชัด → ค้นข้อมูล API ของ NVDA → ออกแบบ → patch → ทดสอบ (แล้วบอกผู้ใช้ว่า autoread.py เปลี่ยนเพื่อ sync NVSky)
2. ทดสอบค้างของ B2c (เพิ่ม/ลบบัญชี, ช่อง background ตามเวลา, sound/tray ต่อบัญชี)
3. B3 (คิวงานเบื้องหลังรวมศูนย์, tray ตามบัญชี, แจ้งข้ามบัญชี, Settings > Updates, README)
4. บั๊ก Notifications โหลดซ้ำ (ถ้าได้ข้อมูลเพิ่ม)
5. สรุปและส่งต่อ NVSky (ผมเขียนให้) พร้อมสรุป autoread เมื่อผู้ใช้ขอ
6. รอบ C
