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
import json
from aiogram.exceptions import TelegramBadRequest
import logging
import time
import sys


class AddDataStates(StatesGroup):
    waiting_for_bank = State()
    waiting_for_opening_date = State()
    waiting_for_sum = State()
    waiting_for_percent = State()
    waiting_for_closing_date = State()
    confirm_data = State()
    waiting_for_delete_choice = State()


load_dotenv()
target_timezone = pytz.timezone("Europe/Moscow")


bot = Bot(getenv("TOKEN"), default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
dp = Dispatcher()
admins = list(map(int, getenv("ADMINS").split(",")))
users = list(map(int, getenv("USERS").split(",")))

allowed_users = set(admins + users)


pd.set_option('future.no_silent_downcasting', True)
DATE_COLS = ["Открытие", "Закрытие"]
FLOAT_COLS = ["Сумма", "Процент", "Итог"]
STR_COL = "Банк"
rows_per_page = 7


NOTIFICATION_DAYS = {1, 3, 7, 30}


def get_user_notified_path(user_id: int) -> str:
    return f"data/{user_id}_notified.json"


def load_notified_events(user_id: int) -> set:
    file_path = get_user_notified_path(user_id)
    if not path.exists(file_path):
        return set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            list_of_tuples = [tuple(item) for item in json.load(f)]
            return set(list_of_tuples)
    except (json.JSONDecodeError, IOError):
        return set()


def save_notified_events(user_id: int, events: set):
    file_path = get_user_notified_path(user_id)
    list_of_tuples = list(events)

    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(list_of_tuples, f, ensure_ascii=False, indent=4)


if not os.path.exists('data'):
    os.makedirs('data')


def get_user_csv_path(user_id: int) -> str:
    return f"data/{user_id}_df.csv"

def get_user_archive_path(user_id: int) -> str:
    return f"data/{user_id}_archive.xlsx"


def prepare_dataframe_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df_display = df.copy()

    current_moment = pd.Timestamp(datetime.datetime.now(tz=target_timezone))
    time_difference = df_display['Закрытие'] - current_moment
    days_array = ceil(time_difference.dt.total_seconds() / (24 * 3600))
    df_display['Осталось'] = pd.Series(days_array, index=df_display.index).astype('Int64')

    for col in DATE_COLS:
        df_display[col] = df_display[col].dt.strftime('%d.%m.%Y')
    for col in FLOAT_COLS:
        df_display[col] = df_display[col].round(2)

    return df_display.fillna('').astype(str)


async def perform_check_logic(user_id: int):
    try:
        df = load_user_data(user_id)
        if df.empty:
            return

        notified_events = load_notified_events(user_id)
        has_new_notifications = False
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

                recipients = set(admins)
                recipients.add(user_id)

                for recipient_id in recipients:
                    await bot.send_message(recipient_id, message_text)

                notified_events.add(event_key)
                has_new_notifications = True

        if has_new_notifications:
            save_notified_events(user_id, notified_events)
            print(f"Файл уведомлений для user_id {user_id} обновлен.")

        indices_to_process = df_check.index[df_check['Осталось'] <= -1].tolist()

        if indices_to_process:
            user_archive_file = get_user_archive_path(user_id)
            user_csv_file = get_user_csv_path(user_id)
            if path.exists(user_archive_file):
                df_archive = pd.read_excel(user_archive_file, parse_dates=["Открытие", "Закрытие"])
                for col in DATE_COLS:
                    if col in df_archive.columns:
                        df_archive[col] = pd.to_datetime(df_archive[col], dayfirst=True, errors='coerce')
                        if df_archive[col].dt.tz is None:
                            df_archive[col] = df_archive[col].dt.tz_localize(target_timezone)
                        else:
                            df_archive[col] = df_archive[col].dt.tz_convert(target_timezone)
                        df_archive[col] = df_archive[col].dt.normalize()
            else:
                archive_cols = [col for col in df.columns if col != 'Осталось']
                df_archive = pd.DataFrame(columns=archive_cols)

            records_to_archive = df.loc[indices_to_process].copy()

            if 'Осталось' in records_to_archive.columns:
                records_to_archive.drop(columns=['Осталось'], inplace=True)

            updated_archive = pd.concat([df_archive, records_to_archive], ignore_index=True)
            updated_archive = updated_archive.sort_values(by="Закрытие", ascending=False, ignore_index=True)
            updated_archive.drop_duplicates(subset=['Банк', 'Открытие', 'Сумма', 'Закрытие'], keep='first', inplace=True)

            for col in DATE_COLS:
                if col in updated_archive.columns and pd.api.types.is_datetime64_any_dtype(updated_archive[col]):
                    updated_archive[col] = updated_archive[col].dt.strftime('%d.%m.%Y')

            updated_archive.to_excel(user_archive_file, index=False)
            print(f"Архив обновлен. Добавлено {len(records_to_archive)} записей. Всего в архиве: {len(updated_archive)}.")

            for index_to_archive in indices_to_process:
                deleted_row_info = df.loc[index_to_archive]
                bank_name = deleted_row_info['Банк']
                closing_date_str = deleted_row_info['Закрытие'].strftime('%d.%m.%Y')

                message_text = (
                    f"ℹ️ *Запись перенесена в архив*\n\n"
                    f"Вклад в банке *'{bank_name}'* \(закрытие `{closing_date_str}`\) был перемещен в архив завершенных вкладов\."
                )

                recipients = set(admins)
                recipients.add(user_id)

                for recipient_id in recipients:
                    await bot.send_message(recipient_id, message_text)

            df.drop(indices_to_process, inplace=True)
            df.reset_index(drop=True, inplace=True)
            df.to_csv(user_csv_file, index=False)
            print("DataFrame сохранен после удаления старых записей.")

    except Exception as e:
        print(f"Ошибка в фоновой задаче perform_check_logic: {e}")
        await asyncio.sleep(300)


async def run_checks_for_all_users():
    print(
        f"[{datetime.datetime.now(target_timezone).strftime('%H:%M:%S')}] Запуск плановой проверки для всех пользователей...")
    try:
        user_files = [f for f in os.listdir('data') if f.endswith('_df.csv')]
        for user_file in user_files:
            try:
                user_id = int(user_file.split('_')[0])
                await perform_check_logic(user_id)
            except Exception as e:
                print(f"Не удалось обработать файл {user_file}: {e}")
    except Exception as e:
        print(f"Критическая ошибка при поиске файлов пользователей: {e}")


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

        await run_checks_for_all_users()


def load_user_data(user_id: int) -> pd.DataFrame:
    user_csv_file = get_user_csv_path(user_id)

    if path.exists(user_csv_file):
        df = pd.read_csv(user_csv_file, parse_dates=DATE_COLS)
        for col in FLOAT_COLS:
            df[col] = pd.to_numeric(df[col], ).astype(float)
        if STR_COL in df.columns:
            df[STR_COL] = df[STR_COL].astype(str)
        for col in DATE_COLS:
            if df[col].dt.tz is None:
                df[col] = df[col].dt.tz_localize(target_timezone)
            else:
                df[col] = df[col].dt.tz_convert(target_timezone)
            df[col] = df[col].dt.normalize()
    else:
        columns_with_types = {
            STR_COL: pd.Series(dtype='str'),
            "Открытие": pd.Series(dtype=f'datetime64[ns, {target_timezone}]'),
            "Сумма": pd.Series(dtype='float'),
            "Процент": pd.Series(dtype='float'),
            "Закрытие": pd.Series(dtype=f'datetime64[ns, {target_timezone}]'),
            "Осталось": pd.Series(dtype='float'),  # Будет NaN
            "Итог": pd.Series(dtype='float')
        }
        df = pd.DataFrame(columns_with_types)
        df.to_csv(user_csv_file, index=False)

    return df


def make_arrows(df: pd.DataFrame, m: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    n = int(ceil(len(df) / rows_per_page)) if not df.empty else 1

    builder.row(
        InlineKeyboardButton(text="<--", callback_data="back"),
        InlineKeyboardButton(text=f"{m + 1}/{n}", callback_data="page"),
        InlineKeyboardButton(text="-->", callback_data="next")
    )
    builder.row(InlineKeyboardButton(text="Файл с таблицей", callback_data="file"))

    return builder


def prepare_display(df_display, curr_page):
    headers = df_display.columns.tolist()

    if df_display.empty:
        table_date = []
    else:
        paginated_df = df_display[curr_page * rows_per_page:(curr_page + 1) * rows_per_page]
        table_date = paginated_df.values.tolist()

    if not table_date:
        return tabulate([], headers=headers,
                        tablefmt="rounded_grid",
                        numalign="center", stralign="center")

    table_simple_grid = tabulate(table_date, headers=headers,
                                 tablefmt="rounded_grid", floatfmt=".2f",
                                 numalign="center", stralign="center",
                                 maxcolwidths=15)

    return table_simple_grid


@dp.message(CommandStart())
async def cmd1(message: Message) -> None:
    if int(message.chat.id) not in allowed_users:
        return

    for i in range(30):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
        except:
            break

    msg = await message.answer(f"👋 Здравствуйте, *{getenv(str(message.chat.id))}*\!\n\nЯ ваш финансовый помощник\.")
    await asyncio.sleep(3)
    await msg.edit_text("⚙️ Выберите действие в меню или с помощью команд\.")


@dp.message(Command("delete"))
async def cmd_delete_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        return

    df = load_user_data(user_id)

    if df.empty:
        print(111)
        await message.answer("ℹ️ У вас нет записей для удаления\.")
        return

    df = df.sort_values(by="Закрытие", ascending=True, ignore_index=True)

    records_list_text = []
    for index, row in df.iterrows():
        closing_date_str = row['Закрытие'].strftime('%d.%m.%Y')
        record_info = (
            f"*{index + 1}*: `{row['Банк']}` "
            f"на сумму `{row['Сумма']:.2f}` "
            f"\(до `{closing_date_str}`\)"
        )
        records_list_text.append(record_info)

    await state.update_data(df_for_delete=df.to_dict('records'))

    final_message = (
            "🗑️ *Выберите запись для удаления*\n\n" +
            "\n".join(records_list_text) +
            "\n\nОтправьте номер записи, которую хотите удалить\. Для отмены отправьте `0`\."
    )

    #final_message = final_message.replace('.', '\.')
    print(final_message)
    await message.answer(final_message)
    await state.set_state(AddDataStates.waiting_for_delete_choice)


@dp.message(Command("table"))
async def cmd2(message: Message) -> None:
    if int(message.chat.id) not in allowed_users:
        return

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
    await message.answer("📊 *Сортировка таблицы*\n\n"
    "Выберите, по какому параметру отсортировать активные вклады:", reply_markup=match.as_markup())


@dp.message(Command("exit"))
async def cmd3(message: Message) -> None:
    if int(message.chat.id) not in allowed_users:
        user_id = message.from_user.id
        user_name = message.from_user.full_name
        print(f"Пользователь {user_name} (ID: {user_id}) попробовал использовать команду /exit.")
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
    if int(message.chat.id) not in allowed_users:
        return

    user_archive_path = get_user_archive_path(message.from_user.id)
    if path.exists(user_archive_path):
        try:
            document = FSInputFile(user_archive_path)
            await message.answer_document(
                document,
                caption="✅ *Архив завершенных вкладов*\."
            )
        except Exception as e:
            await message.answer(f"❌ *Не удалось отправить файл архива:*\n `{e}`")
    else:
        await message.answer("ℹ️ Файл архива еще не создан\. Он появится автоматически, когда завершится первый вклад\.")


def save_xlsx(df_display, file_path: str):
    if path.exists(file_path):
        os.remove(file_path)

    df_excel = df_display.copy()

    if not df_excel.empty:
        current_moment = pd.Timestamp(datetime.datetime.now(tz=target_timezone))
        time_difference = df_excel['Закрытие'] - current_moment
        df_excel['Осталось'] = ceil(time_difference.dt.total_seconds() / (24 * 3600)).astype('Int64')

        for col in DATE_COLS:
            if col in df_excel.columns and pd.api.types.is_datetime64_any_dtype(df_excel[col]):
                df_excel[col] = df_excel[col].dt.strftime('%d.%m.%Y')

    df_excel.to_excel(file_path, index=False)


@dp.callback_query(F.data.in_({"days_up", "days_down", "percent_up", "percent_down",
                               "next", "back", "page", "file"}))
async def callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id not in allowed_users:
        return

    df = load_user_data(user_id)
    session_data = await state.get_data()
    m = session_data.get('m', 0)
    current_sort_by = session_data.get('sort_by')
    current_sort_ascending = session_data.get('sort_asc')

    if df.empty:
        table_title = "📊 *Активные вклады*"
        empty_table_grid = prepare_display(df, 0)

        final_message_text = (
                f"{table_title}\n\n"
                "```\n" + empty_table_grid + "\n```\n"
                                             "_\(У вас пока нет активных вкладов\)_"
        )
        try:
            await callback.message.edit_text(
                final_message_text,
                reply_markup=make_arrows(df, m).as_markup()
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

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
            n = int(ceil(len(df) / rows_per_page)) if not df.empty else 1
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

    await state.update_data(m=m, sort_by=current_sort_by, sort_asc=current_sort_ascending)

    df_to_sort = df.copy()

    if current_sort_by:
        if current_sort_by == "Осталось":
            current_moment = pd.Timestamp(datetime.datetime.now(tz=target_timezone))
            time_difference = df_to_sort['Закрытие'] - current_moment
            df_to_sort['Осталось'] = ceil(time_difference.dt.total_seconds() / (24 * 3600))

        df_to_sort = df_to_sort.sort_values(
            by=current_sort_by,
            ascending=current_sort_ascending,
            ignore_index=True
        )

    if callback.data == "file":
            user_xlsx_file = f"data/{user_id}_Сводка.xlsx"
            save_xlsx(df_to_sort, user_xlsx_file)
            try:
                document = FSInputFile(user_xlsx_file)
                await callback.message.answer_document(
                    document,
                    caption="✅ Сводка по активным вкладам\."
                )
            except Exception as e:
                await callback.message.answer(f"❌ Не удалось отправить файл: {e}")
            await callback.answer()
            return

    df_display = prepare_dataframe_for_display(df_to_sort)

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

    await callback.message.edit_text(
        final_message_text,
        reply_markup=make_arrows(df, m).as_markup()
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if int(message.chat.id) not in allowed_users:
        return

    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Вы сейчас не в процессе ввода данных\.")
    else:
        await state.clear()
        await message.answer("Процесс ввода данных отменен\.")


@dp.message(AddDataStates.waiting_for_delete_choice)
async def process_delete_choice(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        choice = int(message.text)
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число\.")
        return

    if choice == 0:
        await message.answer("✅ Удаление отменено\.")
        await state.clear()
        return

    user_data = await state.get_data()
    df_list = user_data.get('df_for_delete')

    if not df_list or not (0 < choice <= len(df_list)):
        await message.answer(f"❌ Неверный номер\. Введите число от 1 до {len(df_list) if df_list else 'N'}\.")
        return

    record_to_delete = df_list[choice - 1]

    df_full = load_user_data(user_id)

    # Находим индекс строки в "живом" DataFrame, которую нужно удалить
    # Сравниваем по нескольким ключевым полям для надежности
    original_index = df_full[
        (df_full['Банк'] == record_to_delete['Банк']) &
        (df_full['Сумма'] == record_to_delete['Сумма']) &
        # pandas корректно сравнит datetime с таймзоной и без
        (pd.to_datetime(df_full['Закрытие']) == pd.to_datetime(record_to_delete['Закрытие']))
        ].index

    if original_index.empty:
        await message.answer("❌ Не удалось найти запись для удаления\. Возможно, она уже была удалена\.")
        await state.clear()
        return

    # Удаляем строку по найденному индексу
    df_full.drop(original_index, inplace=True)
    df_full.to_csv(get_user_csv_path(user_id), index=False)

    deleted_info = f"`{record_to_delete['Банк']}` на сумму `{record_to_delete['Сумма']:.2f}`"
    await message.answer(f"✅ Запись успешно удалена:\n{deleted_info}")
    await state.clear()


@dp.message(Command("add_date"))
async def cmd_add_data(message: Message, state: FSMContext):
    if int(message.chat.id) not in allowed_users:
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
    if message.from_user.id not in allowed_users:
        await state.clear()
        return

    await state.update_data(bank=message.text)
    await state.set_state(AddDataStates.waiting_for_opening_date)
    await message.answer("❓ Введите дату открытия в формате `ДД\.ММ\.ГГГГ`:")

@dp.message(AddDataStates.waiting_for_opening_date)
async def process_opening_date(message: Message, state: FSMContext):
    if message.from_user.id not in allowed_users:
        await state.clear()
        return

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
    if message.from_user.id not in allowed_users:
        await state.clear()
        return

    try:
        parsed_sum = float(message.text)
        await state.update_data(sum=parsed_sum)
        await state.set_state(AddDataStates.waiting_for_percent)
        await message.answer("❓ Введите процентую ставку \(например, `10\.5` или `18`\):")
    except ValueError:
        await message.answer("❌ *Неверный формат суммы*.\nПожалуйста, введите число:")

@dp.message(AddDataStates.waiting_for_percent)
async def process_percent(message: Message, state: FSMContext):
    if message.from_user.id not in allowed_users:
        await state.clear()
        return

    try:
        pardes_percent = float(message.text)

        await state.update_data(percent=pardes_percent)
        await state.set_state(AddDataStates.waiting_for_closing_date)
        await message.answer("❓ Введите дату закрытия в формате `ДД\.ММ\.ГГГГ`:")
    except ValueError:
        await message.answer("❌ *Неверный формат процента*\.\nПожалуйста, введите число:")

@dp.message(AddDataStates.waiting_for_closing_date)
async def process_closing_date(message: Message, state: FSMContext):
    if message.from_user.id not in allowed_users:
        await state.clear()
        return

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
    user_id = callback_query.from_user.id
    if user_id not in allowed_users:
        await state.clear()
        return

    if callback_query.data == "confirm_add_data":
        user_data = await state.get_data()

        df = load_user_data(user_id)

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
        df.to_csv(get_user_csv_path(user_id), index=False)

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
    await run_checks_for_all_users()
    asyncio.create_task(reminders_scheduler())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    log_level = logging.INFO
    log_format = '%(asctime)s - %(levelname)s - %(message)s'

    logging.basicConfig(level=log_level, format=log_format, stream=sys.stdout)

    file_handler = logging.FileHandler("bot.log", encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(log_format))

    logging.getLogger().addHandler(file_handler)

    while True:
        try:
            logging.info("Запуск бота...")
            asyncio.run(main())
        except KeyboardInterrupt:
            logging.info("Бот остановлен вручную.")
            sys.exit(0)
        except Exception as e:
            logging.error(f"Произошла критическая ошибка: {e}", exc_info=True)
            print(f"Произошла критическая ошибка: {e}. Перезапуск через 15 секунд...")
            time.sleep(15)
