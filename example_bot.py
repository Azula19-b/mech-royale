import mechfight as mf

class Bot:
    name = "idle-control"

    def __init__(self, hello):
        pass

    def act(self, obs):
        return {
            "tick": obs["tick"],
            "move": [0.0, 0.0],
            "boost": False,
            "fire": [False, False],
        }

if __name__ == "__main__":
    mf.run(Bot)
