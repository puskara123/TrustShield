"""
TrustShield Environment Server
Person A owns this file.
"""

from fastapi import FastAPI
import uvicorn
from trustshield.env import TrustShieldEnv

def create_app():
	app = FastAPI(title="TrustShield Environment API")
	env = TrustShieldEnv()

	@app.get("/health")
	async def health():
		return {"status": "healthy"}

	@app.post("/reset")
	async def reset():
		obs = env.reset()
		return obs.model_dump() if hasattr(obs, "model_dump") else obs

	@app.post("/step")
	async def step(action: dict):
		from trustshield.env import AgentAction
		obs = env.step(AgentAction(**action))
		return obs.model_dump() if hasattr(obs, "model_dump") else obs

	return app

if __name__ == "__main__":
	app = create_app()
	uvicorn.run(app, host="0.0.0.0", port=8000)
