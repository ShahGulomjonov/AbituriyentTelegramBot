import json
import logging
import time
import os
from pathlib import Path
import hashlib
import httpx
from flask import Flask, request, jsonify
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    CallbackQueryHandler, MessageHandler, filters
)

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Muhit o'zgaruvchilarini o'qish ---
CLICK_SERVICE_ID = os.environ.get("CLICK_SERVICE_ID")
CLICK_MERCHANT_ID = os.environ.get("CLICK_MERCHANT_ID")
CLICK_SECRET_KEY = os.environ.get("CLICK_SECRET_KEY")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Global o'zgaruvchi: To'lovlarni vaqtincha saqlash uchun
PAYMENT_STATUSES = {}

# --- Veb-server (Flask) qismi ---
app = Flask(__name__)

@app.route('/')
def health_check():
    """Render.com uchun xizmatning "tirik" ekanligini bildiradi."""
    return "Bot and Web Server are running!", 200

@app.route('/click/prepare', methods=['POST'])
def click_prepare():
    try:
        data = request.json
        logger.info(f"CLICK PREPARE so'rovi keldi: {data}")
        response = {
            "click_trans_id": data.get("click_trans_id"), "merchant_trans_id": data.get("merchant_trans_id"),
            "merchant_prepare_id": data.get("merchant_trans_id"), "error": 0, "error_note": "Success"
        }
        return jsonify(response)
    except Exception as e:
        logger.error(f"CLICK PREPARE xatosi: {e}", exc_info=True)
        return jsonify({"error": -8, "error_note": "Error in request"}), 400

@app.route('/click/complete', methods=['POST'])
def click_complete():
    try:
        data = request.json
        logger.info(f"CLICK COMPLETE so'rovi keldi: {data}")
        click_trans_id = data.get("click_trans_id"); merchant_trans_id = data.get("merchant_trans_id")
        amount = data.get("amount"); action = data.get("action"); error = data.get("error")
        sign_time = data.get("sign_time"); sign_string_from_click = data.get("sign_string")

        my_sign_string = f"{click_trans_id}{CLICK_SERVICE_ID}{CLICK_SECRET_KEY}{merchant_trans_id}{amount}{action}{sign_time}"
        encoded_string = hashlib.md5(my_sign_string.encode('utf-8')).hexdigest()

        if encoded_string != sign_string_from_click:
            logger.error("XAVFSIZLIK XATOSI: CLICK'dan kelgan imzo mos kelmadi!")
            return jsonify({"error": -1, "error_note": "SIGN CHECK FAILED!"})
        
        if str(action) == "1" and str(error) == "0":
            chat_id_str = merchant_trans_id.split('-')[1]
            if chat_id_str.isdigit():
                PAYMENT_STATUSES[int(chat_id_str)] = {"status": "paid", "trans_id": merchant_trans_id}
                logger.info(f"Chat ID {chat_id_str} uchun to'lov qabul qilindi.")
            response = {"click_trans_id": click_trans_id, "merchant_trans_id": merchant_trans_id, "error": 0, "error_note": "Success"}
            return jsonify(response)
        else:
            logger.warning(f"To'lov bekor qilindi. Error code: {error}")
            return jsonify({"error": -2, "error_note": "Transaction cancelled"})
    except Exception as e:
        logger.error(f"click_complete funksiyasida kutilmagan xato: {e}", exc_info=True)
        return jsonify({"error": -9, "error_note": "Error in merchant's code"})

# --- Telegram Bot qismi ---
SELECT_PAIR, GET_BALL, AWAITING_PAYMENT_CHECK = range(3)

FANLAR_JUFTLIKLARI = [
    "Biologiya - Kimyo","Biologiya - Ona tili va adabiyoti","Chet tili - Ona tili va adabiyoti","Fizika - Chet tili",
    "Fizika - Matematika","Fransuz tili - Ona tili va adabiyoti","Geografiya - Matematika","Huquqshunoslik - Chet tili",
    "Huquqshunoslik - Ingliz tili","Kasbiy (ijodiy) imtihon - Biologiya","Kasbiy (ijodiy) imtihon - Chet tili",
    "Kasbiy (ijodiy) imtihon - Kasbiy (ijodiy) imtihon","Kasbiy (ijodiy) imtihon - Ingliz tili","Kasbiy (ijodiy) imtihon - Kimyo",
    "Kasbiy (ijodiy) imtihon - Matematika","Kasbiy (ijodiy) imtihon - Ona tili va adabiyoti","Ingliz tili - Matematika",
    "Ingliz tili - Ona tili va adabiyoti","Ingliz tili - Tarix","Kimyo - Biologiya","Kimyo - Fizika","Kimyo - Matematika",
    "Matematika - Biologiya","Matematika - Chet tili","Matematika - Fizika","Matematika - Geografiya","Matematika - Ingliz tili",
    "Matematika - Kimyo","Matematika - Ona tili va adabiyoti","Nemis tili - Ona tili va adabiyoti","Ona tili va adabiyoti - Chet tili",
    "Ona tili va adabiyoti - Ingliz tili","Ona tili va adabiyoti - Matematika","Ona tili va adabiyoti - Tarix",
    "O'zbek tili va adabiyoti - Chet tili","Tarix - Chet tili","Tarix - Geografiya","Tarix - Ingliz tili","Tarix - Matematika",
    "Tarix - Ona tili va adabiyoti"
]

def load_data():
    try:
        current_dir = Path(__file__).parent
        file_path = current_dir / "universities.json"
        with open(file_path, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logger.error(f"DB yuklashda xato: {e}"); return None

def normalize_string(text: str) -> str:
    if not isinstance(text, str): return ""
    if "ijodiy" in text.lower() or "kasbiy" in text.lower(): return "kasbiy (ijodiy) imtihon"
    return text.lower().replace("o‘", "o").replace("o'", "o").strip()
    
def get_minimum_passing_score(user_data: dict, data: dict) -> float | None:
    norm_user_fan1, norm_user_fan2 = normalize_string(user_data['fan1']), normalize_string(user_data['fan2'])
    min_score = float('inf'); found = False
    for otm in data.get('otmlar', []):
        for yo_nalish in otm.get("ta'lim_yo'nalishlari", []):
            fanlar = yo_nalish.get("fanlar", [])
            if len(fanlar) == 2 and fanlar[0].get('tartib') == 1 and fanlar[1].get('tartib') == 2:
                norm_req_fan1, norm_req_fan2 = normalize_string(fanlar[0].get('nomi')), normalize_string(fanlar[1].get('nomi'))
                if norm_user_fan1 == norm_req_fan1 and norm_user_fan2 == norm_req_fan2:
                    kontrakt_scores = yo_nalish.get("o'tish_ballari", {}).get('kontrakt', {})
                    if kontrakt_scores:
                        last_year = max(kontrakt_scores.keys())
                        score = kontrakt_scores[last_year]
                        if score < min_score: min_score = score; found = True
    return min_score if found else None

async def create_click_invoice(amount: str, phone_number: str, merchant_trans_id: str) -> dict:
    url = "https://api.click.uz/v2/merchant/invoice/create"
    timestamp = str(int(time.time()))
    auth_string = f"{timestamp}{CLICK_SECRET_KEY}{CLICK_SERVICE_ID}"
    token = hashlib.sha256(auth_string.encode('utf-8')).hexdigest()
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Auth': f"2:{token}:{timestamp}"}
    payload = {"service_id": int(CLICK_SERVICE_ID), "amount": float(amount), "phone_number": phone_number, "merchant_trans_id": str(merchant_trans_id)}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            logger.info(f"CLICK Invoice API javobi: {response.status_code} - {response.text}")
            return response.json()
    except Exception as e:
        logger.error(f"CLICK Invoice API'ga ulanishda xato: {e}"); return {"error_code": -1, "error_note": "API'ga ulanishda xatolik"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = []
    for pair in FANLAR_JUFTLIKLARI:
        keyboard.append([InlineKeyboardButton(pair, callback_data=pair)])
    await update.message.reply_text("Fanlar juftligini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard)); return SELECT_PAIR

async def select_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    pair_string = query.data
    try:
        fanlar = [s.strip() for s in pair_string.split(" - ")]; fan1 = fanlar[0]; fan2 = fanlar[1] if len(fanlar) > 1 else fan1
    except Exception:
        await query.edit_message_text("Xatolik. /start"); return ConversationHandler.END
    context.user_data['fan1'] = fan1; context.user_data['fan2'] = fan2
    await query.edit_message_text(text=f"✅ Tanlangan juftlik: {pair_string}\n\nBalingizni kiriting (Masalan: 137.0)"); return GET_BALL

async def get_ball(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        if not all([CLICK_SERVICE_ID, CLICK_MERCHANT_ID, CLICK_SECRET_KEY, BOT_USERNAME]):
            await update.message.reply_text("XATOLIK: To'lov tizimi sozlanmagan!")
            return ConversationHandler.END

        ball = float(update.message.text)
        context.user_data['ball'] = ball
        await update.message.reply_text("⏳ Dastlabki tekshiruv...")
        
        data = load_data()
        if not data:
            await update.message.reply_text("Xatolik: Ma'lumotlar bazasi topilmadi."); return ConversationHandler.END

        min_score = get_minimum_passing_score(context.user_data, data)
        if min_score is not None and ball < min_score:
            await update.message.reply_text(f"Balingiz bu yo'nalishdagi eng past o'tish balidan ({min_score}) past.\n\nOmad tilaymiz!"); return ConversationHandler.END

        recommendations = find_recommendations(context.user_data, data)
        context.user_data['recommendations'] = recommendations
        
        await update.message.reply_text("⏳ To'lov yaratilmoqda...")
        merchant_trans_id = f"abt-{update.effective_chat.id}-{int(time.time())}"
        context.user_data['merchant_trans_id'] = merchant_trans_id
        amount = "37000.00"
        
        test_phone_number = "998901234567"
        invoice_data = await create_click_invoice(amount, test_phone_number, merchant_trans_id)

        if invoice_data.get("error_code") == 0:
            invoice_id = invoice_data.get("invoice_id")
            payment_url = f"https://my.click.uz/services/pay?invoice_id={invoice_id}"
            PAYMENT_STATUSES[update.effective_chat.id] = {"status": "new"}
            keyboard = [
                [InlineKeyboardButton("💳 CLICK orqali to'lash", url=payment_url)],
                [InlineKeyboardButton("✅ To'lovni tekshirdim", callback_data="check_payment")]
            ]
            await update.message.reply_text(f"✅ Tavsiyalar tayyor! Natijani ko'rish uchun {int(float(amount))} so'm to'lov qiling.", reply_markup=InlineKeyboardMarkup(keyboard))
            return AWAITING_PAYMENT_CHECK
        else:
            error_note = invoice_data.get("error_note", "Noma'lum xato")
            await update.message.reply_text(f"To'lov yaratishda xatolik yuz berdi: {error_note}")
            return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Xatolik! Ballni raqam bilan kiriting."); return GET_BALL
    except Exception as e:
        logger.error(f"`get_ball` funksiyasida xato: {e}", exc_info=True);
        await update.message.reply_text("Kutilmagan texnik xatolik. /start"); return ConversationHandler.END

async def handle_payment_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat_id = query.message.chat_id
    payment_info = PAYMENT_STATUSES.get(chat_id)
    
    if payment_info and payment_info.get("status") == "paid":
        await query.edit_message_text("✅ To'lov muvaffaqiyatli qabul qilindi! Natijalar:")
        await show_final_results(query, context)
        del PAYMENT_STATUSES[chat_id]
        return ConversationHandler.END
    else:
        await query.answer("❗️ To'lov hali tasdiqlanmadi...", show_alert=True)
        return AWAITING_PAYMENT_CHECK

async def show_final_results(query: Update, context: ContextTypes.DEFAULT_TYPE):
    recommendations = context.user_data.get('recommendations', [])
    user_ball = context.user_data.get('ball')
    message = f"Balingiz: {user_ball}\n\n"
    if not recommendations:
        message += "Afsuski, mos yo'nalishlar topilmadi."
    else:
        message += "Siz uchun 5 ta eng mos yo'nalish:\n\n"
        for i, rec in enumerate(recommendations, 1):
            message += (f"*{i}. {rec['otm_nomi']}*\n  Hudud: {rec['otm_hududi']}\n  Yo'nalish: _{rec['yo_nalish_nomi']}_\n"
                        f"  Ta'lim shakli: {rec['education_form']}\n  Ta'lim tili: {rec['language']}\n"
                        f"  Kontrakt: {rec['kontrakt_miqdori']:,} so'm\n  Status: *{rec['status']}*\n"
                        f"  O'tish bali ({rec['year']}): {rec['passing_score']}\n\n")
    message += "\n Manbaa: www.uzbmb.uz"
    await context.bot.send_message(chat_id=query.message.chat_id, text=message, parse_mode='Markdown')

def find_recommendations(user_data: dict, data: dict) -> list:
    norm_user_fan1, norm_user_fan2 = normalize_string(user_data['fan1']), normalize_string(user_data['fan2'])
    user_ball = user_data['ball']; suitable_directions = []
    for otm in data.get('otmlar', []):
        for yo_nalish in otm.get("ta'lim_yo'nalishlari", []):
            try:
                fanlar = yo_nalish.get("fanlar", [])
                if len(fanlar) == 2 and fanlar[0].get('tartib') == 1 and fanlar[1].get('tartib') == 2:
                    norm_req_fan1, norm_req_fan2 = normalize_string(fanlar[0].get('nomi')), normalize_string(fanlar[1].get('nomi'))
                    if norm_user_fan1 == norm_req_fan1 and norm_user_fan2 == norm_req_fan2:
                        o_tish_ballari = yo_nalish.get("o'tish_ballari", {}); grant_scores, kontrakt_scores = o_tish_ballari.get('grant', {}), o_tish_ballari.get('kontrakt', {})
                        grant_passing_score, kontrakt_passing_score = None, None; grant_year_info, kontrakt_year_info = "", ""
                        if grant_scores:
                            grant_passing_score = (sum(grant_scores.values()) / len(grant_scores)) if len(grant_scores) >= 2 else grant_scores[max(grant_scores.keys())]
                            grant_year_info = f"{len(grant_scores)} yillik o'rtacha" if len(grant_scores) >= 2 else f"{max(grant_scores.keys())}-yil"
                        if kontrakt_scores:
                            kontrakt_passing_score = (sum(kontrakt_scores.values()) / len(kontrakt_scores)) if len(kontrakt_scores) >= 2 else kontrakt_scores[max(kontrakt_scores.keys())]
                            kontrakt_year_info = f"{len(kontrakt_scores)} yillik o'rtacha" if len(kontrakt_scores) >= 2 else f"{max(kontrakt_scores.keys())}-yil"
                        status, passing_score, year_info = None, None, ""
                        if grant_passing_score and user_ball >= grant_passing_score: status, passing_score, year_info = "Grant", grant_passing_score, grant_year_info
                        elif kontrakt_passing_score and user_ball >= kontrakt_passing_score: status, passing_score, year_info = "Kontrakt", kontrakt_passing_score, kontrakt_year_info
                        if status:
                            suitable_directions.append({"otm_nomi": otm.get("otm_nomi", "N/A").capitalize(), "otm_hududi": otm.get("otm_hududi", "N/A"),"yo_nalish_nomi": yo_nalish.get("ta'lim_yo'nalishi_nomi", "N/A"), "status": status,"passing_score": round(passing_score, 1), "year": year_info, "education_form": yo_nalish.get("education_form", "N/A"),"language": yo_nalish.get("language", "N/A"), "kontrakt_miqdori": yo_nalish.get("kontrakt_miqdori", 0)})
            except Exception as e:
                logger.warning(f"Yo'nalish o'qishda xato: {e}"); continue
    return sorted(suitable_directions, key=lambda x: x['passing_score'], reverse=True)[:5]

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Suhbat bekor qilindi.");return ConversationHandler.END

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def main() -> None:
    if not BOT_TOKEN:
        logger.error("DIQQAT: TELEGRAM_BOT_TOKEN muhit o'zgaruvchisi topilmadi!")
        return

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("Flask veb-serveri Render uchun ishga tushdi.")

    application = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_PAIR: [CallbackQueryHandler(select_pair)],
            GET_BALL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ball)],
            AWAITING_PAYMENT_CHECK: [CallbackQueryHandler(handle_payment_check)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)
    logger.info("Telegram Bot polling rejimida ishga tushdi.")
    application.run_polling()

if __name__ == "__main__":
    main()