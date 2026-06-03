"""Setup configuration for b24-ai-auditor package."""

from setuptools import setup, find_packages

setup(
    name="b24-ai-auditor",
    version="0.1.0",
    description="AI-аудитор для Битрикс24 на LangGraph + DeepSeek (через RouterAI)",
    author="Your Name",
    python_requires=">=3.11",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "langgraph>=0.2.0",
        "langchain-openai>=0.2.0",
        "fast-bitrix24>=1.0.0",
        "python-dotenv>=1.0.0",
        "pydantic-settings>=2.0.0",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
