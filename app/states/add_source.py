from aiogram.fsm.state import State, StatesGroup


class AddSourceStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_unsubscribe_link = State()
