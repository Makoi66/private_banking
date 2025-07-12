import os
import pandas as pd
from aiogram import Bot, Dispatcher, F
import asyncio
from os import getenv, path
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from tabulate import tabulate
from aiogram.enums import ParseMode
import datetime
from numpy import ceil, nan
import pytz
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.client.default import DefaultBotProperties


class AddDataStates(StatesGroup):
    waiting_for_bank = State()
    waiting_for_opening_date = State()
    waiting_for_sum = State()
    waiting_for_percent = State()
    waiting_for_closing_date = State()
    confirm_data = State()


load_dotenv()
target_timezone = pytz.timezone("Europe/Moscow")


bot = Bot(getenv("TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
dp = Dispatcher()
admins = list(map(int, getenv("ADMINS").split(",")))


pd.set_option('future.no_silent_downcasting', True)
DATE_COLS = ["Открытие", "Закрытие"]
FLOAT_COLS = ["Сумма", "Процент", "Итог"]
STR_COL = "Банк"


NOTIFICATION_DAYS = {1, 3, 7, 30}
notified_events = set()
ARCHIVE_FILE = "df_archive.xlsx"


async def perform_check_logic():
    global df

    try:
        if df.empty:
            return

        df_check = df.copy()

        current_moment = pd.Timestamp(datetime.datetime.now(tz=target_timezone))
        time_difference = df_check['Закрытие'] - current_moment
        days_left_series = ceil(time_difference.dt.total_seconds() / (24 * 3600))

        df_check['Осталось'] = days_left_series.astype(int)

        reminders_to_send = df_check[df_check['Осталось'].isin(NOTIFICATION_DAYS)]

        for index, row in reminders_to_send.iterrows():
            days_left = row['Осталось']
            bank_name = row['Банк']
            safe_result = f"{row['Итог']:.2f}".replace('.', '\.')
            closing_date_str = row['Закрытие'].strftime('%d.%m.%Y')

            event_key = (bank_name, closing_date_str, days_left, safe_result)

            if event_key not in notified_events:
                message_text = (
                    f"⚠️ *Напоминание\!*\n\n"
                    f"До закрытия вклада в банке *'{bank_name}'* на сумму *{safe_result}* осталось *{days_left}* дн\.\n"
                    f"Дата закрытия: `{closing_date_str}`"
                )

                for admin_id in admins:
                    await bot.send_message(admin_id, message_text)

                notified_events.add(event_key)

        indices_to_process = df_check.index[df_check['Осталось'] <= -1].tolist()

        if indices_to_process:
            if path.exists(ARCHIVE_FILE):
                df_archive = pd.read_excel(ARCHIVE_FILE)
                df_archive['Открытие'] = pd.to_datetime(df_archive['Открытие'])
                df_archive['Закрытие'] = pd.to_datetime(df_archive['Закрытие'])
            else:
                archive_cols = [col for col in df.columns if col != 'Осталось']
                df_archive = pd.DataFrame(columns=archive_cols)

            records_to_archive = df.loc[indices_to_process].copy()

            if 'Осталось' in records_to_archive.columns:
                records_to_archive.drop(columns=['Осталось'], inplace=True)

            updated_archive = pd.concat([df_archive, records_to_archive], ignore_index=True)

            updated_archive = updated_archive.sort_values(by="Закрытие", ascending=False, ignore_index=True)

            updated_archive.drop_duplicates(subset=['Банк', 'Открытие', 'Сумма', 'Закрытие'], keep='first', inplace=True)

            updated_archive.to_excel(ARCHIVE_FILE, index=False)
            print(f"Архив обновлен. Добавлено {len(records_to_archive)} записей. Всего в архиве: {len(updated_archive)}.")

            for index_to_archive  in indices_to_process:
                deleted_row_info = df.loc[index_to_archive]
                bank_name = deleted_row_info['Банк']
                closing_date_str = deleted_row_info['Закрытие'].strftime('%d.%m.%Y')

                message_text = (
                    f"ℹ️ *Запись перенесена в архив*\n\n"
                    f"Вклад в банке *'{bank_name}'* \(закрытие `{closing_date_str}`\) был перемещен в архив завершенных вкладов\."
                )

                for admin_id in admins:
                    await bot.send_message(admin_id, message_text)

            df.drop(indices_to_process, inplace=True)
            df.reset_index(drop=True, inplace=True)
            df.to_csv("df.csv", index=False)
            print("DataFrame сохранен после удаления старых записей.")

    except Exception as e:
        print(f"Ошибка в фоновой задаче perform_check_logic: {e}")
        await asyncio.sleep(300)


async def reminders_scheduler():
    #print("Планировщик задач запущен.")
    while True:
        now = datetime.datetime.now(target_timezone)

        next_run_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

        if now >= next_run_time:
            next_run_time += datetime.timedelta(days=1)

        sleep_seconds = (next_run_time - now).total_seconds()

        # print(f"Следующая плановая проверка в: {next_run_time.strftime('%Y-%m-%d %H:%M:%S')}. "
        #       f"Осталось ждать: {sleep_seconds / 3600:.2f} ч.")

        await asyncio.sleep(sleep_seconds)

        await perform_check_logic()


rows_per_page = 1


def init_dataframe():
    df = pd.DataFrame(columns=[STR_COL, "Открытие", "Сумма", "Процент", "Закрытие", "Осталось", "Итог"])

    if path.exists("df.csv"):
        df = pd.read_csv("df.csv", dayfirst=True)
        for col in FLOAT_COLS:
            df[col] = pd.to_numeric(df[col], ).astype(float)
        if STR_COL in df.columns:
            df[STR_COL] = df[STR_COL].astype(str)
        for col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], dayfirst=True)
            if df[col].dt.tz is None:
                df[col] = df[col].dt.tz_localize(target_timezone)
            else:
                df[col] = df[col].dt.tz_convert(target_timezone)
            df[col] = df[col].dt.normalize()
    else:
        open_date = pd.to_datetime("11.07.2025", format="%d.%m.%Y").tz_localize(target_timezone).normalize()
        close_date = pd.to_datetime("13.07.2025", format="%d.%m.%Y").tz_localize(target_timezone).normalize()
        df.loc[len(df)] = {
            STR_COL: "alpha",
            "Открытие": open_date,
            "Сумма": 30000000.0,
            "Процент": 18.0,
            "Закрытие": close_date,
            "Осталось": nan,
            "Итог": nan
        }
        df["Итог"] = df["Сумма"] * (1 + ((df["Процент"] / (100 * 365)) * (df["Закрытие"] - df["Открытие"]).dt.days))
        for col in FLOAT_COLS:
            df[col] = pd.to_numeric(df[col]).astype(float)
        df.to_csv("df.csv", index=False)

    return df

global df, msg, m, current_sort_by, current_sort_ascending
m = 0
current_sort_by = None
current_sort_ascending = None

def make_arrows():
    global m

    builder = InlineKeyboardBuilder()
    n = int(ceil(len(df) / rows_per_page))

    builder.row(
        InlineKeyboardButton(text="<--", callback_data="back"),
        InlineKeyboardButton(text=f"{m + 1}/{n}", callback_data="page"),
        InlineKeyboardButton(text="-->", callback_data="next")
    )
    builder.row(InlineKeyboardButton(text="Файл с таблицей", callback_data="file"))

    return builder


def prepare_display(df_display, curr_page):
    headers = df_display[curr_page * rows_per_page:(curr_page + 1) * rows_per_page].columns.tolist()
    table_date = df_display[curr_page * rows_per_page:(curr_page + 1) * rows_per_page].values.tolist()

    table_simple_grid = tabulate(table_date, headers=headers,
                                 tablefmt="rounded_grid", floatfmt=".2f",
                                 numalign="center", stralign="center",
                                 maxcolwidths=15)

    return table_simple_grid


@dp.message(CommandStart())
async def cmd1(message: Message) -> None:
    if int(message.chat.id) not in admins:
        return

    global msg
    for i in range(30):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
        except:
            break
    msg = await message.answer(f"👋 Здравствуйте, *{getenv(str(message.chat.id))}*\!\n\nЯ ваш финансовый помощник\.")
    await asyncio.sleep(3)
    msg = await msg.edit_text("⚙️ Выберите действие в меню или с помощью команд\.")


@dp.message(Command("table"))
async def cmd2(message: Message) -> None:
    if int(message.chat.id) not in admins:
        return

    global msg

    for i in range(30):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
        except:
            break

    match = InlineKeyboardBuilder()
    match.row(InlineKeyboardButton(text=f"Оставшиеся дни (по возрастанию ↑)", callback_data="days_up"))
    match.row(InlineKeyboardButton(text=f"Доходности (по убыванию ↓)", callback_data="percent_down"))
    match.row(InlineKeyboardButton(text=f"Оставшиеся дни (по убыванию ↓)", callback_data="days_down"))
    match.row(InlineKeyboardButton(text=f"Доходности (по возрастанию ↑)", callback_data="percent_up"))
    msg = await message.answer("📊 *Сортировка таблицы*\n\n"
    "Выберите, по какому параметру отсортировать активные вклады:", reply_markup=match.as_markup())


@dp.message(Command("exit"))
async def cmd3(message: Message) -> None:
    if int(message.chat.id) not in admins:
        return

    tmp = await message.answer("👋 *Сеанс завершен\.* До свидания\!")
    await asyncio.sleep(5)
    for i in range(30):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
        except:
            await tmp.delete()
            return


@dp.message(Command("archive"))
async def cmd_archive(message: Message):
    if int(message.chat.id) not in admins:
        return

    if path.exists(ARCHIVE_FILE):
        try:
            document = FSInputFile(ARCHIVE_FILE)
            await message.answer_document(
                document,
                caption="✅ *Архив завершенных вкладов*\."
            )
        except Exception as e:
            await message.answer(f"❌ *Не удалось отправить файл архива:*\n `{e}`")
    else:
        await message.answer("ℹ️ Файл архива еще не создан\. Он появится автоматически, когда завершится первый вклад\.")


def save_xlsx(df_display):
    if path.exists("Сводка.xlsx"):
        os.remove("Сводка.xlsx")
    df_excel = df_display.copy()
    df_excel.to_excel("Сводка.xlsx", index=False)


@dp.callback_query(F.data.in_({"days_up", "days_down", "percent_up", "percent_down",
                               "next", "back", "page", "file"}))
async def callback(callback: CallbackQuery):
    if int(callback.message.chat.id) not in admins:
        return

    global df, m, msg, current_sort_by, current_sort_ascending

    df_display = df.copy()
    date_format = "%d.%m.%Y"

    for col in DATE_COLS:
        df_display[col] = df_display[col].dt.strftime(date_format).fillna('')
    current_moment = pd.Timestamp(datetime.datetime.now(tz=target_timezone))
    time_difference = df['Закрытие'] - current_moment
    days_array = ceil(time_difference.dt.total_seconds().values / (24 * 3600))

    df_display['Осталось'] = pd.Series(days_array, index=df_display.index).astype(pd.Int64Dtype()).astype(
        object).fillna('')

    for col in FLOAT_COLS:
        df_display[col] = df_display[col].round(2)
        df_display[col] = df_display[col].astype(object).fillna('')

    match callback.data:
        case "days_up":
            current_sort_by = "Осталось"
            current_sort_ascending = True
            m = 0
        case "days_down":
            current_sort_by = "Осталось"
            current_sort_ascending = False
            m = 0
        case "percent_up":
            current_sort_by = "Процент"
            current_sort_ascending = True
            m = 0
        case "percent_down":
            current_sort_by = "Процент"
            current_sort_ascending = False
            m = 0
        case "next":
            n = int(ceil(len(df_display) / rows_per_page))
            if m < (n - 1):
                m += 1
            else:
                await callback.answer()
                return
        case "back":
            if m > 0:
                m -= 1
            else:
                await callback.answer()
                return
        case "page":
            await callback.answer()
            return
        case "file":
            try:
                document = FSInputFile("Сводка.xlsx")
                await callback.message.answer_document(
                    document,
                    caption="✅ Сводка по активным вкладам\."
                )
            except Exception as e:
                await callback.message.answer(f"❌ Не удалось отправить файл: {e}")
            await callback.answer()
            return

    if current_sort_by:
        df_display = df_display.sort_values(
            by=current_sort_by,
            ascending=current_sort_ascending,
            ignore_index=True
        )
        save_xlsx(df_display)

    table_title = "📊 *Активные вклады*"

    if current_sort_by == "Осталось":
        arrow = "↑" if current_sort_ascending else "↓"
        table_title += f"\n_\(Сортировка: по оставшимся дням {arrow}\)_"
    elif current_sort_by == "Процент":
        arrow = "↑" if current_sort_ascending else "↓"
        table_title += f"\n_\(Сортировка: по доходности {arrow}\)_"

    final_message_text = (
            f"{table_title}\n\n"
            "```\n" + prepare_display(df_display, m) + "\n```"
    )

    await msg.edit_text(
        final_message_text,
        reply_markup=make_arrows().as_markup()
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Вы сейчас не в процессе ввода данных\.")
    else:
        await state.clear()
        await message.answer("Процесс ввода данных отменен\.")


@dp.message(Command("add_date"))
async def cmd_add_data(message: Message, state: FSMContext):
    if int(message.chat.id) not in admins:
        return

    for i in range(30):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
        except:
            break
    await state.set_state(AddDataStates.waiting_for_bank)
    await message.answer("❓ Введине название банка")

@dp.message(AddDataStates.waiting_for_bank)
async def process_bank(message: Message, state: FSMContext):

    await state.update_data(bank=message.text)
    await state.set_state(AddDataStates.waiting_for_opening_date)
    await message.answer("❓ Введите дату открытия в формате `ДД\.ММ\.ГГГГ`:")

@dp.message(AddDataStates.waiting_for_opening_date)
async def process_opening_date(message: Message, state: FSMContext):
    try:
        parsed_date = pd.to_datetime(message.text, format="%d.%m.%Y")
        parsed_date = parsed_date.tz_localize(target_timezone).normalize()

        await state.update_data(opening_date=parsed_date)
        await state.set_state(AddDataStates.waiting_for_sum)
        await message.answer("❓ Введите сумму \(например `1234\.56`\):")
    except ValueError:
        await message.answer("❌ *Неверный формат даты*.\nПожалуйста, введите дату в формате `ДД\.ММ\.ГГГГ`:")

@dp.message(AddDataStates.waiting_for_sum)
async def process_sum(message: Message, state: FSMContext):
    try:
        parsed_sum = float(message.text)
        await state.update_data(sum=parsed_sum)
        await state.set_state(AddDataStates.waiting_for_percent)
        await message.answer("❓ Введите процентую ставку \(например, `10\.5` или `18`\):")
    except ValueError:
        await message.answer("❌ *Неверный формат суммы*.\nПожалуйста, введите число:")

@dp.message(AddDataStates.waiting_for_percent)
async def process_percent(message: Message, state: FSMContext):
    try:
        pardes_percent = float(message.text)

        await state.update_data(percent=pardes_percent)
        await state.set_state(AddDataStates.waiting_for_closing_date)
        await message.answer("❓ Введите дату закрытия в формате `ДД\.ММ\.ГГГГ`:")
    except ValueError:
        await message.answer("❌ *Неверный формат процента*\.\nПожалуйста, введите число:")

@dp.message(AddDataStates.waiting_for_closing_date)
async def process_closing_date(message: Message, state: FSMContext):
    try:
        parsed_date = pd.to_datetime(message.text, format="%d.%m.%Y")
        parsed_date = parsed_date.tz_localize(target_timezone).normalize()

        await state.update_data(closing_date=parsed_date)

        user_data = await state.get_data()
        confirmation_text = (
            "📝 *Пожалуйста, проверьте введенные данные:*\n\n"
            f"*Банк:* `{user_data['bank']}`\n"
            f"*Открытие:* `{user_data['opening_date'].strftime('%d.%m.%Y')}`\n"
            f"*Сумма:* `{user_data['sum']:.2f}`\n"
            f"*Процент:* `{user_data['percent']:.2f}%`\n"
            f"*Закрытие:* `{user_data['closing_date'].strftime('%d.%m.%Y')}`\n\n"
            "Всё верно?"
        )

        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="✅ Да", callback_data="confirm_add_data"))
        builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_data"))

        await message.answer(confirmation_text, reply_markup=builder.as_markup())
        await state.set_state(AddDataStates.confirm_data)
    except ValueError:
        await message.answer("Неверный формат даты\. Пожалуйста, введите дату в формате ДД\.ММ\.ГГГГ:")

@dp.callback_query(F.data.in_({"confirm_add_data", "cancel_add_data"}), AddDataStates.confirm_data)
async def confirm_cancel_add_data(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.data == "confirm_add_data":
        user_data = await state.get_data()

        global df
        new_row = {
            STR_COL: user_data['bank'],
            "Открытие": user_data['opening_date'],
            "Сумма": user_data['sum'],
            "Процент": user_data['percent'],
            "Закрытие": user_data['closing_date'],
            "Осталось": nan,
            "Итог": user_data['sum'] * (1 + ((user_data['percent'] / (100 * 365)) * (user_data['closing_date'] - user_data['opening_date']).days))
        }
        df.loc[len(df)] = new_row
        df.to_csv("df.csv", index=False)
        await callback_query.message.edit_text("✅ *Успешно\!*\n\nДанные о новом вкладе добавлены\.")
    else:
        await callback_query.message.edit_text("❌ *Отменено\.*\n\nДобавление данных прервано\.")
    await state.clear()
    await callback_query.answer()

    await asyncio.sleep(5)
    for i in range(30):
        try:
            await bot.delete_message(callback_query.message.chat.id, callback_query.message.message_id - i)
        except:
            return


async def main() -> None:
    print("Выполняется первоначальная проверка при запуске...")
    await perform_check_logic()

    asyncio.create_task(reminders_scheduler())

    await dp.start_polling(bot)


if __name__ == "__main__":
    global df

    df = init_dataframe()
    asyncio.run(main())
