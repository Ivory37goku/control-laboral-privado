MI CONTROL LABORAL CON IA — WEB APP
====================================

QUÉ ES
- Aplicación web adaptable a móvil y ordenador.
- Dashboard con gráfico de horas extra, resumen de dinero y accesos rápidos.
- Registros de horas extra, viajes, pagos recibidos y copias de seguridad.
- Edición de jornadas, viajes y pagos ya guardados.
- Exportación CSV para abrir con Excel.
- Asistente IA opcional mediante API key.
- Protección CSRF en formularios y validación de integridad de las copias descargables.
- Restauración de copias con validación y copia de seguridad previa automática.
- Contraseña de acceso mediante variable de entorno.

DATOS PRECARGADOS
Se precargan una sola vez:
- Septiembre 2026: 18,5 h extra pendientes (registro agregado al 30/09; fecha aproximada).
- Octubre 2026: 21 h extra provisionales (al 26/10/2026).
- Dos viajes de octubre de 2026 a 200 € cada uno, pendientes (fechas aproximadas).
Las fechas aproximadas están anotadas para que puedas corregirlas. No se incluyen datos reales de Sesame.

PROBAR EN TU ORDENADOR
1. Instala Python 3.11 o posterior.
2. Descomprime el ZIP y abre una terminal dentro de la carpeta.
3. Crea entorno virtual (recomendado):
   python -m venv .venv
   Windows: .venv\Scripts\activate
   macOS/Linux: source .venv/bin/activate
4. Instala dependencias:
   pip install -r requirements.txt
5. Define variables de entorno antes de arrancar:
   Windows PowerShell:
     $env:APP_PASSWORD="una-contraseña-larga"
     $env:SECRET_KEY="una-clave-aleatoria-larga"
     python app.py
   macOS/Linux:
     export APP_PASSWORD='una-contraseña-larga'
     export SECRET_KEY='una-clave-aleatoria-larga'
     python app.py
6. Abre http://127.0.0.1:5000 en ese ordenador.
   Para móvil en la misma red Wi-Fi se puede configurar la app para escuchar en la LAN,
   pero esto no equivale a tener acceso desde cualquier lugar y no debe publicarse sin HTTPS.

MÓVIL + ORDENADOR CON LOS MISMOS DATOS
Para compartir los datos, la aplicación tiene que estar desplegada en un servidor al que
accedan ambos dispositivos. Este ZIP incluye render.yaml preparado para Render con disco
persistente. Pasos generales:
1. Sube el contenido a un repositorio privado de GitHub.
2. En Render crea un servicio nuevo desde ese repositorio y aplica render.yaml.
3. Define APP_PASSWORD con una contraseña larga y OPENAI_API_KEY si quieres usar IA.
4. Comprueba que el servicio usa disco persistente montado en /var/data.
5. Abre la URL HTTPS del servicio en móvil y ordenador e inicia sesión con la misma contraseña.

IMPORTANTE: No he desplegado la app por ti ni he creado un servidor/cuenta. El código está
preparado para desplegarse; compartir datos entre dispositivos solo funcionará después del
despliegue en un servidor persistente. No pongas el archivo de base de datos ni claves en
un repositorio público. Protege la cuenta de hosting y crea copias periódicas.

ACTIVAR IA
- La app funciona sin IA.
- Para habilitarla, configura OPENAI_API_KEY como variable de entorno secreta en el servidor.
- Opcionalmente, configura OPENAI_MODEL (por defecto gpt-4.1-mini).
- La clave de API no debe ponerse en el código ni enviarse por chat.
- En esta primera versión la IA solo lee una selección de registros para responder; no puede
  crear, editar ni borrar registros. Puede resumir jornadas, viajes, pagos y totales. Usa el
  formulario normal para cambiar los datos.
- El uso de API puede generar costes separados.

SEGURIDAD Y LIMITACIONES
- Usa HTTPS al desplegar.
- Cambia SECRET_KEY y configura APP_PASSWORD.
- En Render se activa la cookie de sesión segura; en uso local HTTP no se fuerza para que
  puedas iniciar sesión en localhost.
- El acceso actual es una contraseña compartida; no es un sistema multiusuario con cuentas
  independientes.
- SQLite está en el disco persistente del servidor; no es una sincronización entre dos copias.
- No se conecta automáticamente a Sesame. Importar datos requiere exportación o integración
  autorizada disponible por parte de tu empresa.
- La app es un registro personal, no determina por sí sola el cálculo legal de nóminas.

