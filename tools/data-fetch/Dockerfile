FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

COPY requirements.txt /opt/app-root/src/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py db.py models.py queries.py main.py /opt/app-root/src/data_fetch_tool/

EXPOSE 8080

ENTRYPOINT ["uvicorn", "data_fetch_tool.main:app", "--host", "0.0.0.0", "--port", "8080"]
