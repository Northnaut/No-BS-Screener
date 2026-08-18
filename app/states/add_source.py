from aiogram.fsm.state import State, StatesGroup


class AddSourceStates(StatesGroup):
    waiting_for_link = State()
